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
"""The relational auto-configuration through a real ``ApplicationContext`` on SQLite files.

What an operator sees from the beans (``AsyncEngine``, ``NamedDataSources``, the module stores):

- every engine gets the same treatment (foreign keys, pool settings) and ``ctx.stop()`` disposes all
  of them (F9, proof p9); module URLs resolve through the registry, one engine per database;
- ``echo`` from an env var is typed (C114) and named datasource URLs resolve placeholders (C046);
- relational enabled with no URL fails at startup instead of writing to ``./app.db`` (C043);
- the existing beans are views over the ``DataSourceRegistry`` bean.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Integer, event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.named_datasources import NamedDataSources
from pyfly.data.relational.routing import RoutingSessionFactory
from pyfly.data.relational.sqlalchemy.entity import Base


def _config(relational: dict[str, Any], **root: Any) -> Config:
    tree: dict[str, Any] = {"data": {"relational": {"enabled": "true", "ddl-auto": "none", **relational}}}
    tree.update(root)
    return Config({"pyfly": tree})


def _sqlite(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _scalar(engine: AsyncEngine, sql: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


async def _started(config: Config) -> ApplicationContext:
    context = ApplicationContext(config)
    await context.start()
    return context


class TestEveryEngineIsConfigured:
    async def test_primary_engine_enforces_foreign_keys(self, tmp_path: Path) -> None:
        context = await _started(_config({"url": _sqlite(tmp_path / "app.db")}))
        try:
            engine = context.get_bean(AsyncEngine)
            assert await _scalar(engine, "PRAGMA foreign_keys") == 1
            assert await _scalar(engine, "PRAGMA journal_mode") == "wal"
        finally:
            await context.stop()

    async def test_named_and_replica_engines_get_the_pool_and_sqlite_settings(self, tmp_path: Path) -> None:
        context = await _started(
            _config(
                {
                    "url": _sqlite(tmp_path / "app.db"),
                    "pool": {"size": "4", "recycle": "300"},
                    "read-replica": {"url": _sqlite(tmp_path / "replica.db")},
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "reporting.db")}},
                }
            )
        )
        try:
            reporting = context.get_bean(NamedDataSources).get("reporting")
            replica = context.get_bean(RoutingSessionFactory).replica()
            try:
                for engine in (context.get_bean(AsyncEngine), reporting.kw["bind"], replica.bind):
                    assert engine.pool.size() == 4
                    assert engine.pool._recycle == 300
                    assert await _scalar(engine, "PRAGMA foreign_keys") == 1
            finally:
                await replica.close()
        finally:
            await context.stop()

    async def test_env_echo_false_keeps_echo_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PYFLY_DATA_RELATIONAL_ECHO", "false")
        context = await _started(
            _config(
                {
                    "url": _sqlite(tmp_path / "app.db"),
                    "read-replica": {"url": _sqlite(tmp_path / "replica.db")},
                }
            )
        )
        try:
            assert context.get_bean(AsyncEngine).echo is False
            replica = context.get_bean(RoutingSessionFactory).replica()
            assert replica.bind.echo is False
            await replica.close()
        finally:
            await context.stop()

    async def test_named_datasource_url_placeholders_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REPORTING_DIR", str(tmp_path))
        context = await _started(
            _config(
                {
                    "url": _sqlite(tmp_path / "app.db"),
                    "datasources": {"reporting": {"url": "sqlite+aiosqlite:///${REPORTING_DIR}/reporting.db"}},
                }
            )
        )
        try:
            factory: async_sessionmaker[AsyncSession] = context.get_bean(NamedDataSources).get("reporting")
            async with factory() as session:
                assert (await session.execute(text("SELECT 1"))).scalar() == 1
            assert (tmp_path / "reporting.db").exists()
        finally:
            await context.stop()


class TestMissingUrl:
    async def test_relational_enabled_without_url_fails_at_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        context = ApplicationContext(_config({}))
        with pytest.raises(Exception, match=r"pyfly\.data\.relational\.url"):
            await context.start()
        await context.stop()
        assert not (tmp_path / "app.db").exists()


class TestDisposal:
    """Proof p9: one engine per database, every engine configured, every engine disposed on stop."""

    async def test_modules_share_the_primary_and_stop_disposes_every_engine(self, tmp_path: Path) -> None:
        url = _sqlite(tmp_path / "app.db")
        context = await _started(
            _config(
                {
                    "url": url,
                    "read-replica": {"url": url},
                    "datasources": {"reporting": {"url": url}},
                },
                eventsourcing={
                    "enabled": "true",
                    "store": {"provider": "sqlalchemy"},
                    "snapshot": {"provider": "sqlalchemy", "url": url},
                },
                transactional={"enabled": "true", "persistence": {"provider": "sqlalchemy"}},
            )
        )
        from pyfly.data.relational.datasource_registry import DataSourceRegistry
        from pyfly.eventsourcing.snapshot import SnapshotStore
        from pyfly.eventsourcing.store import EventStore
        from pyfly.transactional.core.persistence import ExecutionPersistenceProvider

        registry = context.get_bean(DataSourceRegistry)
        engines = [ds.engine for ds in registry.all_datasources()]
        disposed: list[int] = []
        for engine in engines:
            event.listen(engine.sync_engine, "engine_disposed", lambda e: disposed.append(id(e)))
        try:
            primary = context.get_bean(AsyncEngine)
            assert registry.primary.engine is primary
            # primary, its replica, and the explicitly named datasource: three pools, not seven.
            assert [ds.qualified_name for ds in registry.all_datasources()] == [
                "primary",
                "primary.replica",
                "reporting",
            ]
            assert context.get_bean(EventStore)._engine is primary  # type: ignore[attr-defined]
            assert context.get_bean(SnapshotStore)._engine is primary  # type: ignore[attr-defined]
            assert context.get_bean(ExecutionPersistenceProvider)._engine is primary  # type: ignore[attr-defined]
        finally:
            await context.stop()
        assert sorted(disposed) == sorted(id(engine.sync_engine) for engine in engines)
        assert registry.closed

    async def test_a_module_on_another_database_gets_a_configured_datasource(self, tmp_path: Path) -> None:
        context = await _started(
            _config(
                {"url": _sqlite(tmp_path / "app.db"), "pool": {"size": 2}},
                eventsourcing={
                    "enabled": "true",
                    "store": {"provider": "sqlalchemy", "url": _sqlite(tmp_path / "events.db")},
                    "snapshot": {"provider": "sqlalchemy", "url": _sqlite(tmp_path / "events.db")},
                },
            )
        )
        from pyfly.data.relational.datasource_registry import DataSourceRegistry
        from pyfly.eventsourcing.snapshot import SnapshotStore
        from pyfly.eventsourcing.store import EventStore

        try:
            registry = context.get_bean(DataSourceRegistry)
            events = registry.get("event-store")
            assert context.get_bean(EventStore)._engine is events.engine  # type: ignore[attr-defined]
            assert context.get_bean(SnapshotStore)._engine is events.engine  # type: ignore[attr-defined]
            assert events.engine.pool.size() == 2
            assert events.url_key == "pyfly.eventsourcing.store.url"
            assert await _scalar(events.engine, "PRAGMA foreign_keys") == 1
        finally:
            await context.stop()
        assert registry.closed

    async def test_restart_builds_a_fresh_registry(self, tmp_path: Path) -> None:
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = _config({"url": _sqlite(tmp_path / "app.db")})
        context = await _started(config)
        first = context.get_bean(DataSourceRegistry)
        await context.stop()
        await context.start()
        try:
            second = context.get_bean(DataSourceRegistry)
            assert second is not first and first.closed and not second.closed
            assert await _scalar(context.get_bean(AsyncEngine), "SELECT 1") == 1
        finally:
            await context.stop()


class StopOrderItem(Base):
    """A table ``ddl-auto=create-drop`` creates on start and drops on stop."""

    __tablename__ = "registry_stop_order_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class TestStopOrder:
    async def test_create_drop_runs_before_the_registry_disposes_the_engine(self, tmp_path: Path) -> None:
        context = await _started(_config({"url": _sqlite(tmp_path / "app.db"), "ddl-auto": "create-drop"}))
        engine = context.get_bean(AsyncEngine)
        assert await _scalar(engine, f"SELECT count(*) FROM {StopOrderItem.__tablename__}") == 0
        disposed: list[int] = []
        event.listen(engine.sync_engine, "engine_disposed", lambda e: disposed.append(id(e)))
        await context.stop()
        # The schema was dropped on the live pool, then the registry disposed it: no pool left open.
        assert disposed == [id(engine.sync_engine)]
        assert engine.pool.checkedin() == 0
        with pytest.raises(Exception, match="no such table"):
            await _scalar(engine, f"SELECT count(*) FROM {StopOrderItem.__tablename__}")
        await engine.dispose()


class TestBeansAreViewsOverTheRegistry:
    async def test_existing_beans_keep_their_names_and_types(self, tmp_path: Path) -> None:
        from pyfly.data.relational.auto_configuration import EngineLifecycle
        from pyfly.data.relational.datasource_registry import DataSourceRegistry
        from pyfly.data.relational.health import SqlAlchemyHealthIndicator

        config = _config(
            {
                "url": _sqlite(tmp_path / "app.db"),
                "read-replica": {"url": _sqlite(tmp_path / "replica.db")},
                "datasources": {"reporting": {"url": _sqlite(tmp_path / "reporting.db")}},
            }
        )
        context = await _started(config)
        try:
            registry = context.get_bean(DataSourceRegistry)
            assert registry is DataSourceRegistry.for_config(config)
            assert context.get_bean_by_name("datasource_registry") is registry
            assert context.get_bean_by_name("async_engine") is registry.primary.engine
            assert context.get_bean_by_name("async_session_factory") is registry.primary.sessionmaker
            routing = context.get_bean_by_name("routing_session_factory")
            assert routing._primary is registry.primary.sessionmaker
            replica = registry.replica()
            assert replica is not None and routing._replica is replica.sessionmaker
            named = context.get_bean_by_name("named_data_sources")
            assert named.names() == ["reporting"]
            assert named.get("reporting") is registry.get("reporting").sessionmaker
            assert isinstance(context.get_bean_by_name("engine_lifecycle"), EngineLifecycle)
            assert isinstance(context.get_bean_by_name("db_health_indicator"), SqlAlchemyHealthIndicator)
            first = context.get_bean(AsyncSession)
            second = context.get_bean(AsyncSession)
            assert first is not second and first.bind is registry.primary.engine
            await first.close()
            await second.close()
        finally:
            await context.stop()


class TestSpiBeans:
    async def test_customizer_and_credentials_beans_are_registered(self, tmp_path: Path) -> None:
        from pyfly.container.stereotypes import component
        from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry

        @component
        class TenantGuc:
            async def after_begin(self, connection: Any, datasource: DataSource) -> None:
                await connection.execute(text("SELECT 1"))

        @component
        class ReportingOnly:
            datasources = ("reporting",)

            async def after_begin(self, connection: Any, datasource: DataSource) -> None:
                return None

        @component
        class Vault:
            def datasource_credentials(self, datasource: str) -> tuple[str | None, str | None] | None:
                return None

        @component
        class NotACustomizer:
            # A synchronous method of the same name is not the SPI.
            def after_begin(self, connection: Any, datasource: DataSource) -> None:
                return None

        context = ApplicationContext(
            _config(
                {
                    "url": _sqlite(tmp_path / "app.db"),
                    "datasources": {"reporting": {"url": _sqlite(tmp_path / "reporting.db")}},
                }
            )
        )
        for bean in (TenantGuc, ReportingOnly, Vault, NotACustomizer):
            context.register_bean(bean)
        await context.start()
        try:
            registry = context.get_bean(DataSourceRegistry)
            guc = context.get_bean(TenantGuc)
            reporting_only = context.get_bean(ReportingOnly)
            assert registry.primary.customizers == (guc,)
            assert set(map(id, registry.get("reporting").customizers)) == {id(guc), id(reporting_only)}
            assert registry._credentials_providers == [context.get_bean(Vault)]
        finally:
            await context.stop()
