# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The datasource registry on real SQLite file databases (the server lanes live in tests/integration).

- F9: one registry builds every engine, one engine per distinct URL, the same pool settings on every
  engine, and ``close()`` disposes each engine exactly once.
- F13: connect arguments pass through, ``pool.recycle`` defaults to 1800 s, pre-ping stays off.
- C044/C116: SQLite enforces foreign keys, runs WAL with ``synchronous=NORMAL`` and a busy timeout on
  file databases, and emits ``BEGIN`` itself, so isolation is real and write units take the write lock
  with ``BEGIN IMMEDIATE``.
- C043: a missing URL fails fast instead of silently opening ``./app.db`` (outside the dev profile).
- The after-begin customizer SPI runs inside the unit's transaction.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String, event, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool

from pyfly.container.ordering import order
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import (
    PRIMARY,
    DataSource,
    DataSourceCapabilities,
    DataSourceConfigurationError,
    DataSourceRegistry,
    NoSuchDataSourceError,
    datasource_of,
)
from pyfly.data.relational.dialect_customizers import SQLITE_BEGIN_OPTION, run_after_begin


class _Base(DeclarativeBase):
    pass


class Account(_Base):
    __tablename__ = "registry_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    balance: Mapped[int] = mapped_column(Integer)


class Parent(_Base):
    __tablename__ = "registry_parent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32))


class Child(_Base):
    __tablename__ = "registry_child"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("registry_parent.id", ondelete="CASCADE"))


def _config(relational: dict[str, Any], **root: Any) -> Config:
    return Config({"pyfly": {"data": {"relational": relational}, **root}})


def _sqlite(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.fixture
async def registries() -> AsyncIterator[list[DataSourceRegistry]]:
    """Registries a test builds; closed afterwards so no pool outlives the test."""
    built: list[DataSourceRegistry] = []
    yield built
    for registry in built:
        await registry.close()


def _registry(built: list[DataSourceRegistry], config: Config) -> DataSourceRegistry:
    registry = DataSourceRegistry.for_config(config)
    built.append(registry)
    return registry


async def _scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


def _statements(engine: AsyncEngine) -> list[str]:
    seen: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        words = statement.split()
        # "BEGIN IMMEDIATE" is recorded whole; any other statement by its verb.
        seen.append(" ".join(words).upper() if words[0].upper() == "BEGIN" else words[0].upper())

    return seen


# ---------------------------------------------------------------------------
# Building from configuration
# ---------------------------------------------------------------------------


class TestBuilding:
    async def test_primary_replica_and_named_datasources(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "primary.db"),
                    "read-replica": {"url": _sqlite(tmp_path / "replica.db")},
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "reporting.db")}},
                }
            ),
        )
        assert registry.names() == [PRIMARY, "reporting"]
        assert registry.primary.name == PRIMARY
        assert registry.get("reporting").url.database == str(tmp_path / "reporting.db")
        replica = registry.replica()
        assert replica is not None and replica.is_replica and replica.name == PRIMARY
        assert replica.qualified_name == "primary.replica"
        assert registry.replica("reporting") is None
        assert registry.engine() is registry.primary.engine
        assert registry.session_factory("reporting") is registry.get("reporting").sessionmaker
        assert len(registry) == 2 and "reporting" in registry
        assert [ds.qualified_name for ds in registry.all_datasources()] == ["primary", "primary.replica", "reporting"]
        with pytest.raises(NoSuchDataSourceError, match="reporting"):
            registry.get("missing")

    async def test_named_datasource_defined_only_in_the_environment(
        self, tmp_path: Path, registries: list[DataSourceRegistry], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # C046: a PYFLY_* variable declares a named datasource with no YAML entry at all.
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_URL", _sqlite(tmp_path / "analytics.db"))
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_POOL_SIZE", "4")
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        assert registry.names() == [PRIMARY, "analytics"]
        analytics = registry.get("analytics")
        assert analytics.url.database == str(tmp_path / "analytics.db")
        assert analytics.engine.pool.size() == 4
        assert await _scalar(analytics.engine, "SELECT 1") == 1

    async def test_url_secrets_are_masked(self, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(registries, _config({"url": "postgresql+asyncpg://app:hunter2@db.internal/orders"}))
        primary = registry.primary
        assert "hunter2" not in repr(primary)
        assert "hunter2" not in primary.masked_url
        assert "hunter2" not in str(primary.url)
        assert primary.masked_url == "postgresql+asyncpg://app:***@db.internal/orders"

    async def test_one_registry_per_config(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        config = _config({"url": _sqlite(tmp_path / "p.db")})
        registry = _registry(registries, config)
        assert DataSourceRegistry.for_config(config) is registry
        assert DataSourceRegistry.for_config(_config({"url": _sqlite(tmp_path / "p.db")})) is not registry

    async def test_datasource_of_maps_engines_and_session_factories(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        primary = registry.primary
        assert datasource_of(primary.engine) is primary
        assert datasource_of(primary.sessionmaker) is primary
        assert registry.find_by_engine(primary.engine) is primary
        foreign = create_async_engine("sqlite+aiosqlite://")
        try:
            assert datasource_of(foreign) is None
        finally:
            await foreign.dispose()


class TestMissingUrl:
    """C043: no URL is an error, not a silent ``./app.db`` in the working directory."""

    async def test_primary_without_url_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registries: list[DataSourceRegistry]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        registry = _registry(registries, _config({"enabled": "true"}))
        with pytest.raises(DataSourceConfigurationError, match=r"pyfly\.data\.relational\.url"):
            _ = registry.primary
        assert registry.has_primary is False
        assert list(tmp_path.iterdir()) == []

    async def test_module_without_url_names_both_keys(self, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(registries, Config({}))
        with pytest.raises(DataSourceConfigurationError, match=r"pyfly\.eventsourcing\.store\.url"):
            registry.resolve(None, name="event-store", url_key="pyfly.eventsourcing.store.url")

    async def test_dev_profile_falls_back_to_an_embedded_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registries: list[DataSourceRegistry]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        registry = _registry(registries, _config({"enabled": "true"}, profiles={"active": "dev"}))
        assert registry.primary.url.get_backend_name() == "sqlite"
        assert registry.primary.url.database == "./app.db"

    async def test_legacy_url_key_builds_the_primary(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        config = Config({"pyfly": {"data": {"url": _sqlite(tmp_path / "legacy.db"), "relational": {"enabled": True}}}})
        registry = _registry(registries, config)
        assert registry.primary.url.database == str(tmp_path / "legacy.db")
        assert registry.primary.url_key == "pyfly.data.url"


# ---------------------------------------------------------------------------
# One treatment for every engine (F9, F13)
# ---------------------------------------------------------------------------


class TestEngineTreatment:
    async def test_every_engine_gets_the_pool_settings(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "p.db"),
                    "pool": {"size": "3", "max-overflow": "1", "timeout": "4", "recycle": "600", "pre-ping": "true"},
                    "read-replica": {"url": _sqlite(tmp_path / "r.db")},
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "rep.db")}},
                }
            ),
        )
        extra = registry.resolve(_sqlite(tmp_path / "events.db"), name="event-store")
        engines = [ds.engine for ds in registry.all_datasources()]
        assert len(engines) == 4 and extra.engine in engines
        for engine in engines:
            pool = engine.pool
            assert isinstance(pool, AsyncAdaptedQueuePool)
            assert pool.size() == 3
            assert pool._max_overflow == 1
            assert pool._timeout == 4.0
            assert pool._recycle == 600
            assert pool._pre_ping is True

    async def test_defaults_recycle_without_pre_ping(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        pool = registry.primary.engine.pool
        assert pool._recycle == 1800
        assert pool._pre_ping is False

    async def test_memory_database_keeps_static_pool_and_is_never_recycled(
        self, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": "sqlite+aiosqlite:///:memory:", "pool": {"size": 3}}))
        engine = registry.primary.engine
        assert isinstance(engine.pool, StaticPool)
        assert engine.pool._recycle == -1  # recycling the one connection would drop the database
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER)"))
        assert await _scalar(engine, "SELECT count(*) FROM t") == 0

    async def test_connect_args_pass_through(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        # sqlite3's own ``timeout`` is a connect argument; the busy_timeout PRAGMA then leaves it alone.
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db"), "connect-args": {"timeout": 12}}))
        assert await _scalar(registry.primary.engine, "PRAGMA busy_timeout") == 12000

    async def test_echo_false_string_keeps_echo_off(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "p.db"),
                    "echo": "false",
                    "read-replica": {"url": _sqlite(tmp_path / "r.db")},
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "rep.db"), "echo": "false"}},
                }
            ),
        )
        assert [ds.engine.echo for ds in registry.all_datasources()] == [False, False, False]

    async def test_session_factories_do_not_expire_on_commit(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        assert registry.primary.sessionmaker.kw["expire_on_commit"] is False


class TestModuleUrls:
    """Module URLs are aliases resolved through the registry: one engine per database."""

    async def test_no_url_is_the_primary(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        assert registry.resolve(None, name="event-store") is registry.primary

    async def test_identical_url_reuses_the_engine(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "p.db"),
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "rep.db")}},
                }
            ),
        )
        assert registry.resolve(_sqlite(tmp_path / "p.db"), name="cache") is registry.primary
        assert registry.resolve(_sqlite(tmp_path / "rep.db"), name="cache") is registry.get("reporting")
        assert registry.names() == [PRIMARY, "reporting"]

    async def test_relative_and_absolute_paths_are_one_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registries: list[DataSourceRegistry]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        registry = _registry(registries, _config({"url": "sqlite+aiosqlite:///./p.db"}))
        assert registry.find_by_url(_sqlite(tmp_path / "p.db")) is registry.primary

    async def test_another_url_registers_a_named_datasource(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db"), "pool": {"size": 2}}))
        events = registry.resolve(_sqlite(tmp_path / "events.db"), name="event-store", url_key="pyfly.x.url")
        assert registry.names() == [PRIMARY, "event-store"]
        assert events.url_key == "pyfly.x.url"
        assert events.engine.pool.size() == 2
        # A second module on the same database shares it.
        assert registry.resolve(_sqlite(tmp_path / "events.db"), name="snapshot-store") is events

    async def test_a_name_cannot_move_to_another_database(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        registry.register("cache", _sqlite(tmp_path / "c1.db"))
        assert registry.register("cache", _sqlite(tmp_path / "c1.db")) is registry.get("cache")
        with pytest.raises(DataSourceConfigurationError, match="cache"):
            registry.register("cache", _sqlite(tmp_path / "c2.db"))
        with pytest.raises(DataSourceConfigurationError, match="reserved"):
            registry.register(PRIMARY, _sqlite(tmp_path / "c3.db"))

    async def test_primary_is_not_required_for_a_module_url(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(registries, Config({}))
        store = registry.resolve(_sqlite(tmp_path / "events.db"), name="event-store")
        assert registry.names() == ["event-store"]
        assert store.name == "event-store"


class TestClose:
    async def test_close_disposes_every_engine_exactly_once(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        config = _config(
            {
                "url": _sqlite(tmp_path / "p.db"),
                "read-replica": {"url": _sqlite(tmp_path / "r.db")},
                "datasources": {"reporting": {"url": _sqlite(tmp_path / "rep.db")}},
            }
        )
        registry = _registry(registries, config)
        registry.resolve(_sqlite(tmp_path / "events.db"), name="event-store")
        disposed: list[int] = []
        engines = [ds.engine for ds in registry.all_datasources()]
        for engine in engines:
            event.listen(engine.sync_engine, "engine_disposed", lambda e: disposed.append(id(e)))
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            assert engine.pool.checkedin() == 1

        await registry.close()
        await registry.close()  # idempotent

        assert sorted(disposed) == sorted(id(engine.sync_engine) for engine in engines)
        assert all(engine.pool.checkedin() == 0 for engine in engines)
        assert registry.closed
        with pytest.raises(DataSourceConfigurationError, match="closed"):
            registry.register("late", _sqlite(tmp_path / "late.db"))
        assert DataSourceRegistry.for_config(config) is not registry  # a restart builds a fresh one
        await DataSourceRegistry.for_config(config).close()

    async def test_a_closed_and_dropped_registry_releases_its_engines(self, tmp_path: Path) -> None:
        config = _config({"url": _sqlite(tmp_path / "p.db"), "datasources": {"r": {"url": _sqlite(tmp_path / "r.db")}}})
        registry = DataSourceRegistry.for_config(config)
        engines = [weakref.ref(ds.engine) for ds in registry.all_datasources()]
        configs = weakref.ref(config)
        await _scalar(registry.primary.engine, "SELECT 1")
        await registry.close()
        del registry, config
        gc.collect()
        assert [ref() for ref in engines] == [None, None]
        assert configs() is None


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    async def test_sqlite(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(registries, _config({"url": _sqlite(tmp_path / "p.db")}))
        caps = registry.primary.capabilities
        assert caps.dialect == "sqlite" and caps.driver == "aiosqlite"
        assert caps.supports_savepoints is True
        assert caps.fast_autocommit_reads is False
        assert caps.isolation_levels == frozenset({"READ UNCOMMITTED", "SERIALIZABLE"})
        assert caps.supports_isolation("serializable") and not caps.supports_isolation("READ COMMITTED")
        assert caps.max_in_params in (999, 32766)

    @pytest.mark.parametrize(
        ("url", "dialect", "fast", "levels"),
        [
            (
                "postgresql+asyncpg://u@h/db",
                "postgresql",
                True,
                {"READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"},
            ),
            (
                "mysql+asyncmy://u@h/db",
                "mysql",
                False,
                {"READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"},
            ),
            (
                "mariadb+asyncmy://u@h/db",
                "mariadb",
                False,
                {"READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE"},
            ),
        ],
    )
    async def test_server_dialects_before_connecting(
        self, url: str, dialect: str, fast: bool, levels: set[str], registries: list[DataSourceRegistry]
    ) -> None:
        # asyncpg has no READ UNCOMMITTED; only PostgreSQL gets the autocommit-read accelerator.
        caps = _registry(registries, _config({"url": url})).primary.capabilities
        assert caps.dialect == dialect
        assert caps.fast_autocommit_reads is fast
        assert caps.isolation_levels == frozenset(levels)
        assert caps.supports_savepoints is True

    def test_of_a_dialect(self) -> None:
        engine = create_async_engine("postgresql+asyncpg://u@h/db")
        caps = DataSourceCapabilities.of(engine.dialect)
        assert caps.supports_returning and caps.max_in_params == 32767


# ---------------------------------------------------------------------------
# SQLite correctness (C044, C116)
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_file(tmp_path: Path, registries: list[DataSourceRegistry]) -> DataSource:
    registry = _registry(registries, _config({"url": _sqlite(tmp_path / "app.db")}))
    datasource = registry.primary
    async with datasource.engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    return datasource


class TestSqliteSetup:
    async def test_file_database_pragmas(self, sqlite_file: DataSource) -> None:
        engine = sqlite_file.engine
        assert await _scalar(engine, "PRAGMA foreign_keys") == 1
        assert await _scalar(engine, "PRAGMA journal_mode") == "wal"
        assert await _scalar(engine, "PRAGMA synchronous") == 1  # NORMAL
        assert await _scalar(engine, "PRAGMA busy_timeout") == 5000

    async def test_memory_database_enforces_foreign_keys(self, registries: list[DataSourceRegistry]) -> None:
        engine = _registry(registries, _config({"url": "sqlite+aiosqlite://"})).primary.engine
        assert await _scalar(engine, "PRAGMA foreign_keys") == 1

    async def test_settings_are_configurable(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "p.db"),
                    "sqlite": {"journal-mode": "delete", "synchronous": "full", "busy-timeout": 750},
                }
            ),
        )
        engine = registry.primary.engine
        assert await _scalar(engine, "PRAGMA journal_mode") == "delete"
        assert await _scalar(engine, "PRAGMA synchronous") == 2
        assert await _scalar(engine, "PRAGMA busy_timeout") == 750

    async def test_orphan_rows_are_rejected(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.sessionmaker() as session:
            session.add(Child(id=1, parent_id=999_999))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_on_delete_cascade_runs(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.sessionmaker.begin() as session:
            session.add(Parent(id=1, name="alice"))
            await session.flush()
            session.add_all([Child(id=10, parent_id=1), Child(id=11, parent_id=1)])
        async with sqlite_file.engine.begin() as conn:
            await conn.execute(text("DELETE FROM registry_parent WHERE id = 1"))
        assert await _scalar(sqlite_file.engine, "SELECT count(*) FROM registry_child") == 0


class TestSqliteBegin:
    """The pysqlite/aiosqlite BEGIN recipe: reads run inside the transaction."""

    async def test_begin_precedes_the_first_read(self, sqlite_file: DataSource) -> None:
        seen = _statements(sqlite_file.engine)
        async with sqlite_file.sessionmaker.begin() as session:
            await session.execute(select(Account))
            await session.execute(select(Account))
        assert seen == ["BEGIN", "SELECT", "SELECT"]

    async def test_write_units_begin_immediate(self, sqlite_file: DataSource) -> None:
        seen = _statements(sqlite_file.engine)
        async with sqlite_file.sessionmaker() as session:
            await session.connection(execution_options=sqlite_file.begin_options(read_only=False))
            session.add(Account(id=1, balance=100))
            await session.commit()
        assert seen[0] == "BEGIN IMMEDIATE"
        assert sqlite_file.begin_options(read_only=True) == {}
        assert sqlite_file.begin_options(read_only=False) == {SQLITE_BEGIN_OPTION: "IMMEDIATE"}

    async def test_concurrent_write_units_serialize_instead_of_losing_an_update(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.sessionmaker.begin() as session:
            session.add(Account(id=1, balance=100))

        async def add_ten(pause: float) -> str:
            async with sqlite_file.sessionmaker() as session:
                await session.connection(execution_options=sqlite_file.begin_options(read_only=False))
                account = (await session.execute(select(Account).where(Account.id == 1))).scalar_one()
                await asyncio.sleep(pause)
                account.balance += 10
                await session.commit()
                return "ok"

        results = await asyncio.gather(add_ten(0.2), add_ten(0.0))
        assert results == ["ok", "ok"]
        assert await _scalar(sqlite_file.engine, "SELECT balance FROM registry_account WHERE id = 1") == 120

    async def test_deferred_units_never_lose_an_update(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.sessionmaker.begin() as session:
            session.add(Account(id=1, balance=100))

        async def add_ten(pause: float) -> str:
            try:
                async with sqlite_file.sessionmaker() as session:
                    account = (await session.execute(select(Account).where(Account.id == 1))).scalar_one()
                    await asyncio.sleep(pause)
                    account.balance += 10
                    await session.commit()
                    return "ok"
            except OperationalError:
                return "busy"

        results = await asyncio.gather(add_ten(0.2), add_ten(0.05))
        balance = await _scalar(sqlite_file.engine, "SELECT balance FROM registry_account WHERE id = 1")
        # Without the recipe both commit and the balance is 110: a lost update.
        assert balance == 100 + 10 * results.count("ok")
        assert "busy" in results

    async def test_savepoints_roll_back_alone(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.sessionmaker() as session, session.begin():
            session.add(Account(id=1, balance=1))
            await session.flush()
            nested = await session.begin_nested()
            session.add(Account(id=2, balance=2))
            await session.flush()
            await nested.rollback()
        assert await _scalar(sqlite_file.engine, "SELECT group_concat(id) FROM registry_account") == "1"

    async def test_autocommit_connections_emit_no_begin_and_the_next_transaction_still_does(
        self, sqlite_file: DataSource
    ) -> None:
        seen = _statements(sqlite_file.engine)
        async with sqlite_file.engine.connect() as conn:
            auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await auto.execute(text("INSERT INTO registry_account (id, balance) VALUES (1, 5)"))
        assert seen == ["INSERT"]
        seen.clear()
        async with sqlite_file.engine.connect() as conn:  # same pooled connection, reset by the pool
            await conn.execute(text("UPDATE registry_account SET balance = 6"))
            await conn.rollback()
        assert seen == ["BEGIN", "UPDATE"]
        assert await _scalar(sqlite_file.engine, "SELECT balance FROM registry_account") == 5

    async def test_memory_database_sessions_sharing_the_one_connection_do_not_collide(
        self, registries: list[DataSourceRegistry]
    ) -> None:
        # StaticPool hands every session the same connection. A session left open after a read (a
        # repository's long-lived session) keeps that connection in a transaction; a second session's
        # BEGIN must join it, as the driver did, instead of failing "within a transaction".
        datasource = _registry(registries, _config({"url": "sqlite+aiosqlite:///:memory:"})).primary
        async with datasource.engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        lingering = datasource.sessionmaker()
        try:
            await lingering.execute(select(Account))
            async with datasource.sessionmaker.begin() as session:
                session.add(Account(id=1, balance=10))
            async with datasource.sessionmaker() as session:
                assert (await session.execute(select(Account.balance))).scalar_one() == 10
        finally:
            await lingering.close()

    async def test_an_isolation_level_does_not_bring_the_driver_begin_back(self, sqlite_file: DataSource) -> None:
        seen = _statements(sqlite_file.engine)
        async with sqlite_file.sessionmaker() as session:
            await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})
            session.add(Account(id=1, balance=1))
            await session.commit()
        async with sqlite_file.sessionmaker.begin() as session:
            session.add(Account(id=2, balance=2))
        assert seen.count("BEGIN") == 2
        assert await _scalar(sqlite_file.engine, "SELECT count(*) FROM registry_account") == 2


# ---------------------------------------------------------------------------
# After-begin customizers
# ---------------------------------------------------------------------------


class _Recorder:
    """Writes a row inside the transaction it customizes, so a rollback proves where it ran."""

    def __init__(self, label: str, log: list[str]) -> None:
        self.label = label
        self.log = log

    async def after_begin(self, connection: AsyncSession | AsyncConnection, datasource: DataSource) -> None:
        self.log.append(f"{self.label}@{datasource.qualified_name}:{connection.in_transaction()}")
        await connection.execute(
            text("INSERT INTO registry_parent (id, name) VALUES (:id, :name)"),
            {"id": len(self.log), "name": self.label},
        )


@order(2)
class _Second(_Recorder):
    pass


@order(1)
class _First(_Recorder):
    pass


@order(3)
class _Third(_Recorder):
    pass


class TestAfterBeginCustomizers:
    async def test_customizer_runs_inside_the_unit(self, sqlite_file: DataSource) -> None:
        log: list[str] = []
        registry = sqlite_file.registry
        assert registry is not None
        registry.add_customizer(_Recorder("tenant", log))

        async with sqlite_file.sessionmaker() as session:
            await session.connection()
            await run_after_begin(sqlite_file, session)
            await session.rollback()
        assert log == ["tenant@primary:True"]
        assert await _scalar(sqlite_file.engine, "SELECT count(*) FROM registry_parent") == 0  # rolled back with it

        async with sqlite_file.engine.begin() as conn:
            await sqlite_file.after_begin(conn)
        assert await _scalar(sqlite_file.engine, "SELECT count(*) FROM registry_parent") == 1  # committed with it

    async def test_order_and_scope(self, tmp_path: Path, registries: list[DataSourceRegistry]) -> None:
        log: list[str] = []
        registry = _registry(
            registries,
            _config(
                {
                    "url": _sqlite(tmp_path / "p.db"),
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "rep.db")}},
                }
            ),
        )
        registry.add_customizer(_Second("second", log))
        registry.add_customizer(_First("first", log))
        registry.add_customizer(_Third("reporting-only", log), datasource="reporting")
        assert [type(c).__name__ for c in registry.primary.customizers] == ["_First", "_Second"]
        assert [c.label for c in registry.get("reporting").customizers] == ["first", "second", "reporting-only"]  # type: ignore[attr-defined]

    async def test_customizers_reach_later_datasources_and_replicas(
        self, tmp_path: Path, registries: list[DataSourceRegistry]
    ) -> None:
        registry = _registry(
            registries,
            _config({"url": _sqlite(tmp_path / "p.db"), "read-replica": {"url": _sqlite(tmp_path / "r.db")}}),
        )
        everywhere = _Recorder("all", [])
        registry.add_customizer(everywhere)
        replica = registry.replica()
        assert replica is not None and replica.customizers == (everywhere,)
        later = registry.resolve(_sqlite(tmp_path / "events.db"), name="event-store")
        assert later.customizers == (everywhere,)

    async def test_no_customizer_is_a_no_op(self, sqlite_file: DataSource) -> None:
        async with sqlite_file.engine.begin() as conn:
            await run_after_begin(sqlite_file, conn)
