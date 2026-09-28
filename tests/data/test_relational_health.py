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
"""Tests for SqlAlchemyHealthIndicator and EngineLifecycle.

No Docker required: SQLite engines, an unreachable port and a local TCP server that accepts and never
answers (a database that went silent) exercise every path.

C021: the ``db`` indicator belongs to the readiness probe only, answers within its timeout even when
the database is silent or the pool is exhausted, and checks every registry datasource.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import String as SAString
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.util import await_only

from pyfly.actuator.health import HealthAggregator, HealthStatus, ProbeGroup
from pyfly.actuator.wiring import install_health_indicators
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.auto_configuration import EngineLifecycle
from pyfly.data.relational.datasource_registry import DataSourceRegistry, observing_checkouts
from pyfly.data.relational.health import SqlAlchemyHealthIndicator, _Check, _select_one
from pyfly.data.relational.sqlalchemy.entity import BaseEntity


@pytest.fixture
async def silent_database() -> AsyncIterator[str]:
    """A TCP server that accepts connections and never says a word: a database gone silent."""
    held: list[asyncio.StreamWriter] = []

    async def _hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    server = await asyncio.start_server(_hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"postgresql+asyncpg://app:secret@127.0.0.1:{port}/orders"
    finally:
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# SqlAlchemyHealthIndicator
# ---------------------------------------------------------------------------


class TestSqlAlchemyHealthIndicatorUp:
    @pytest.mark.asyncio
    async def test_sqlite_memory_reports_up(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            indicator = SqlAlchemyHealthIndicator(engine)
            result: HealthStatus = await indicator.health()
        finally:
            await engine.dispose()

        assert result.status == "UP"

    @pytest.mark.asyncio
    async def test_up_details_include_dialect(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            result = await SqlAlchemyHealthIndicator(engine).health()
        finally:
            await engine.dispose()

        assert "database" in result.details
        assert result.details["database"] == "sqlite"

    @pytest.mark.asyncio
    async def test_returns_health_status_instance(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            result = await SqlAlchemyHealthIndicator(engine).health()
        finally:
            await engine.dispose()

        assert isinstance(result, HealthStatus)


class TestSqlAlchemyHealthIndicatorDown:
    @pytest.mark.asyncio
    async def test_unreachable_postgres_reports_down(self) -> None:
        # Port 1 is effectively unreachable on any normal host; asyncpg will
        # raise a connection error before the pyfly timeout.
        engine = create_async_engine(
            "postgresql+asyncpg://bad:bad@127.0.0.1:1/nope",
            # Connect timeout so the test doesn't hang for the OS default
            connect_args={"timeout": 1},
        )
        try:
            result = await SqlAlchemyHealthIndicator(engine).health()
        finally:
            await engine.dispose()

        assert result.status == "DOWN"
        assert "error" in result.details

    @pytest.mark.asyncio
    async def test_down_details_include_error_type(self) -> None:
        engine = create_async_engine(
            "postgresql+asyncpg://bad:bad@127.0.0.1:1/nope",
            connect_args={"timeout": 1},
        )
        try:
            result = await SqlAlchemyHealthIndicator(engine).health()
        finally:
            await engine.dispose()

        assert result.status == "DOWN"
        # details must contain at least "error" (exception class name)
        assert result.details.get("error"), "DOWN status must carry error class name"

    @pytest.mark.asyncio
    async def test_down_details_include_message(self) -> None:
        engine = create_async_engine(
            "postgresql+asyncpg://bad:bad@127.0.0.1:1/nope",
            connect_args={"timeout": 1},
        )
        try:
            result = await SqlAlchemyHealthIndicator(engine).health()
        finally:
            await engine.dispose()

        # details["message"] may be empty string but the key must exist
        assert "message" in result.details


# ---------------------------------------------------------------------------
# EngineLifecycle — ddl-auto variants
# ---------------------------------------------------------------------------


class _Canary(BaseEntity):
    """Canary table: we probe whether it was created/dropped."""

    __tablename__ = "canary_lifecycle_test"

    label: Mapped[str] = mapped_column(SAString(100), default="x")


class TestEngineLifecycleDdlCreate:
    """ddl-auto='create' — tables are created on start(), never dropped on stop()."""

    @pytest.mark.asyncio
    async def test_start_creates_tables(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = session_factory()

        lifecycle = EngineLifecycle(engine, session, ddl_auto="create")
        try:
            await lifecycle.start()

            # Probe: the canary table must exist
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
                assert result is not None
        finally:
            # Manually drop so we don't pollute the shared Base.metadata
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: _Canary.__table__.drop(c, checkfirst=True))
            await lifecycle.stop()

    @pytest.mark.asyncio
    async def test_stop_does_not_drop_tables_for_create_mode(self, tmp_path: Path) -> None:
        # File-based SQLite so the DB survives engine.dispose() and we can re-inspect it.
        url = f"sqlite+aiosqlite:///{tmp_path / 'ddl_create.db'}"
        engine = create_async_engine(url)
        session: AsyncSession = async_sessionmaker(engine, expire_on_commit=False)()

        lifecycle = EngineLifecycle(engine, session, ddl_auto="create")
        await lifecycle.start()
        await lifecycle.stop()  # "create" mode must NOT drop on stop

        verify = create_async_engine(url)
        try:
            async with verify.connect() as conn:
                # Table must STILL exist — stop() did not drop it.
                result = await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
                assert result is not None
        finally:
            await verify.dispose()


class TestEngineLifecycleDdlCreateDrop:
    """ddl-auto='create-drop' — tables created on start(), dropped on stop()."""

    @pytest.mark.asyncio
    async def test_stop_drops_tables(self, tmp_path: Path) -> None:
        # File-based SQLite so the DB survives engine.dispose() and we can prove drop_all ran.
        url = f"sqlite+aiosqlite:///{tmp_path / 'ddl_create_drop.db'}"
        engine = create_async_engine(url)
        session: AsyncSession = async_sessionmaker(engine, expire_on_commit=False)()

        lifecycle = EngineLifecycle(engine, session, ddl_auto="create-drop")
        await lifecycle.start()

        # Table must exist after start.
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
            assert result is not None

        await lifecycle.stop()  # disposes the engine AND drops all tables

        # Reconnect to the SAME database file — the table must now be GONE.
        verify = create_async_engine(url)
        try:
            async with verify.connect() as conn:
                with pytest.raises(OperationalError):
                    await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
        finally:
            await verify.dispose()


class TestEngineLifecycleDdlNone:
    """ddl-auto='none' — start() must not create any tables."""

    @pytest.mark.asyncio
    async def test_start_does_not_create_tables(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = session_factory()

        lifecycle = EngineLifecycle(engine, session, ddl_auto="none")
        try:
            await lifecycle.start()

            # The canary table must NOT exist
            async with engine.connect() as conn:
                with pytest.raises(OperationalError):
                    await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
        finally:
            await lifecycle.stop()

    def test_invalid_ddl_auto_is_refused(self) -> None:
        """An unknown ddl-auto value fails when the lifecycle is built (it used to become 'create' silently)."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = session_factory()

        with pytest.raises(ValueError, match="ddl-auto must be one of none, validate, create, create-drop"):
            EngineLifecycle(engine, session, ddl_auto="bogus_value")


# ---------------------------------------------------------------------------
# C021 — readiness only, bounded, every datasource
# ---------------------------------------------------------------------------


class TestProbeGroup:
    async def test_db_indicator_is_readiness_only(self, tmp_path: Path) -> None:
        context = ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "data": {
                            "relational": {
                                "enabled": "true",
                                "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                                "ddl-auto": "none",
                            }
                        }
                    }
                }
            )
        )
        await context.start()
        try:
            aggregator = HealthAggregator()
            install_health_indicators(context, aggregator)
            liveness = await aggregator.check_liveness()
            readiness = await aggregator.check_readiness()
            assert "db_health_indicator" not in liveness.components
            assert readiness.components["db_health_indicator"].status == "UP"
            assert "db_health_indicator" in (await aggregator.check()).components
        finally:
            await context.stop()

    def test_indicator_declares_readiness(self) -> None:
        assert SqlAlchemyHealthIndicator.probe_groups == frozenset({ProbeGroup.READINESS})

    async def test_explicit_groups_still_win(self, tmp_path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        try:
            aggregator = HealthAggregator()
            aggregator.add_indicator("db", SqlAlchemyHealthIndicator(engine), groups={ProbeGroup.LIVENESS})
            assert "db" in (await aggregator.check_liveness()).components
        finally:
            await engine.dispose()

    async def test_scan_does_not_register_an_indicator_twice(self, tmp_path: Path) -> None:
        # The documented workaround registers the bean's instance as "db" (readiness) before the scan;
        # the scan used to add it again under its bean name, back in liveness.
        context = ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "data": {
                            "relational": {
                                "enabled": "true",
                                "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                                "ddl-auto": "none",
                            }
                        }
                    }
                }
            )
        )
        await context.start()
        try:
            aggregator = HealthAggregator()
            indicator = context.get_bean(SqlAlchemyHealthIndicator)
            aggregator.add_indicator("db", indicator, groups={ProbeGroup.READINESS})
            install_health_indicators(context, aggregator)
            assert set((await aggregator.check()).components) == {"db"}
        finally:
            await context.stop()


class TestBounded:
    async def test_silent_database_answers_within_the_timeout(self, silent_database: str) -> None:
        engine = create_async_engine(silent_database)
        try:
            started = time.monotonic()
            result = await SqlAlchemyHealthIndicator(engine, timeout=0.3).health()
            elapsed = time.monotonic() - started
        finally:
            await engine.dispose()
        assert result.status == "DOWN"
        assert result.details["error"] == "TimeoutError"
        assert elapsed < 2.0

    async def test_exhausted_pool_is_not_waited_on(self, tmp_path: Path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}", pool_size=1, max_overflow=0, pool_timeout=30
        )
        held = await engine.connect()
        try:
            started = time.monotonic()
            result = await SqlAlchemyHealthIndicator(engine, timeout=5.0).health()
            elapsed = time.monotonic() - started
        finally:
            await held.close()
            await engine.dispose()
        assert elapsed < 1.0
        assert result.status == "UNKNOWN"
        assert result.details["validation"] == "skipped: pool exhausted"
        assert engine.pool.checkedout() == 0

    async def test_concurrent_probes_share_one_check(self, tmp_path: Path) -> None:
        # The readiness probe and a monitoring scrape arriving together borrow one connection, not two.
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        checkouts: list[object] = []
        event.listen(engine.sync_engine, "checkout", lambda *args: checkouts.append(args))
        indicator = SqlAlchemyHealthIndicator(engine)
        try:
            results = await asyncio.gather(indicator.health(), indicator.health(), indicator.health())
            assert [result.status for result in results] == ["UP", "UP", "UP"]
            assert len(checkouts) == 1
            assert (await indicator.health()).status == "UP"  # a finished check is not reused
            assert len(checkouts) == 2
        finally:
            await engine.dispose()

    async def test_check_returns_its_connection(self, tmp_path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
        try:
            await SqlAlchemyHealthIndicator(engine).health()
            assert engine.pool.checkedout() == 0
        finally:
            await engine.dispose()


class TestSharedConnection:
    async def test_a_late_check_leaves_a_shared_in_memory_connection_alone(self) -> None:
        # SQLite :memory: keeps one connection (StaticPool) that the check shares with the application.
        # Stopping the late check must not close it: that would lose the database under the application.
        registry = DataSourceRegistry(
            Config({"pyfly": {"data": {"relational": {"url": "sqlite+aiosqlite:///:memory:"}}}})
        )
        engine = registry.primary.engine
        assert isinstance(engine.pool, StaticPool)

        def _add_pause(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.create_function("pause", 1, time.sleep)  # holds the connection's worker thread

        event.listen(engine.sync_engine, "connect", _add_pause)
        indicator = SqlAlchemyHealthIndicator(engine, registry=registry, timeout=0.2)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE ledger (id INTEGER PRIMARY KEY)"))
                await conn.execute(text("INSERT INTO ledger (id) VALUES (1)"))

            async def _long_statement() -> int:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT pause(0.6)"))
                    return int((await conn.execute(text("SELECT count(*) FROM ledger"))).scalar_one())

            work = asyncio.ensure_future(_long_statement())
            await asyncio.sleep(0.05)
            late = await indicator.health()
            assert late.status == "DOWN"
            assert late.details["error"] == "TimeoutError"

            assert await work == 1  # the application's work finished on its connection
            deadline = time.monotonic() + 2
            while (after := await indicator.health()).status != "UP" and time.monotonic() < deadline:
                await asyncio.sleep(0.05)  # the late check ends by itself, after the application's statement
            assert after.status == "UP", after.details
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT count(*) FROM ledger"))).scalar_one() == 1
        finally:
            await registry.close()


async def _up_within(indicator: SqlAlchemyHealthIndicator, seconds: float) -> HealthStatus:
    """Probe until *indicator* answers UP or *seconds* have passed; the last answer."""
    deadline = time.monotonic() + seconds
    while (answer := await indicator.health()).status != "UP" and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return answer


class _StuckCheck:
    """An engine whose check does not end, not even when cancelled, until ``release`` is set.

    ``stuck_in="checkout"`` stands for a pre-ping on a pooled connection whose flow a middlebox forgot,
    on a pool that does not report the connection it is checking out: cancelling asyncpg's pre-ping makes
    it wait for the dead socket without a timeout, and the check holds no connection it could close.
    ``stuck_in="statement"`` is a check that holds its connection and waits in its ``SELECT 1``.
    """

    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, stuck_in: str) -> None:
        self.stuck_in = stuck_in
        self.release = asyncio.Event()
        self.checkouts = 0

    def connect(self) -> _StuckCheck:
        return self

    async def __aenter__(self) -> _StuckCheck:
        self.checkouts += 1
        if self.stuck_in == "checkout":
            await self._wait_for_release()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    @property
    def sync_connection(self) -> SimpleNamespace:
        # The pooled connection the check holds; ``dbapi_connection`` None: there is no socket to close.
        return SimpleNamespace(connection=SimpleNamespace(dbapi_connection=None))

    async def execute(self, _statement: object) -> None:
        if self.stuck_in == "statement":
            await self._wait_for_release()

    async def _wait_for_release(self) -> None:
        while not self.release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.release.wait()


class TestLateChecks:
    async def test_a_check_stuck_before_it_holds_a_connection_does_not_block_the_next_probes(self) -> None:
        engine = _StuckCheck("checkout")
        indicator = SqlAlchemyHealthIndicator(engine, timeout=0.1)
        try:
            first = await indicator.health()
            assert first.status == "DOWN"
            assert first.details["message"] == "no answer within 0.1 s"

            # The late check never got a connection, so the next probe starts a check of its own (on a
            # pool, another connection) instead of answering DOWN until the stuck one ends.
            second = await indicator.health()
            assert second.status == "DOWN"
            assert second.details["message"] == "no answer within 0.1 s"
            assert engine.checkouts == 2

            # Two late checks are still running: the next probe answers at once and starts none.
            started = time.monotonic()
            third = await indicator.health()
            assert time.monotonic() - started < 0.05
            assert third.status == "DOWN"
            assert third.details["message"].startswith("previous check still running")
            assert engine.checkouts == 2

            engine.release.set()
            assert (await _up_within(indicator, 2)).status == "UP"
        finally:
            engine.release.set()  # checks that ignore cancellation must not outlive a failed assertion

    async def test_a_late_check_that_holds_its_connection_is_waited_for(self) -> None:
        engine = _StuckCheck("statement")
        indicator = SqlAlchemyHealthIndicator(engine, timeout=0.1)
        try:
            first = await indicator.health()
            assert first.status == "DOWN"
            assert first.details["message"] == "no answer within 0.1 s"

            # It borrowed a connection: the next probe borrows no other one while the late check winds down.
            second = await indicator.health()
            assert second.status == "DOWN"
            assert second.details["message"].startswith("previous check still running")
            assert engine.checkouts == 1

            engine.release.set()
            assert (await _up_within(indicator, 2)).status == "UP"
        finally:
            engine.release.set()


def _sqlite_registry(path: Path, *, pre_ping: bool = False) -> DataSourceRegistry:
    """A registry whose primary is a SQLite file, on the registry's pool (``MeteredAsyncQueuePool``)."""
    relational = {"url": f"sqlite+aiosqlite:///{path}", "pool": {"pre-ping": pre_ping}}
    return DataSourceRegistry(Config({"pyfly": {"data": {"relational": relational}}}))


async def _raw_connection(engine: Any) -> Any:
    """Check a connection out, return its DBAPI connection, and give it back."""
    async with engine.connect() as conn:
        return conn.sync_connection.connection.dbapi_connection


class TestCheckoutEntry:
    """The pool entry a check is checking out: the registry's pool reports it, before the pre-ping."""

    async def test_the_registry_pool_reports_the_connection_before_its_pre_ping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A check that hangs in the pre-ping holds no connection yet; the pool entry it is checking out
        # is what lets the late check close the socket the pre-ping waits on.
        registry = _sqlite_registry(tmp_path / "ping.db", pre_ping=True)
        engine = registry.primary.engine
        reported: list[Any] = []
        known_at_ping: list[list[Any]] = []
        ping = engine.dialect.do_ping

        def _recording_ping(dbapi_connection: Any) -> bool:
            known_at_ping.append([entry.dbapi_connection for entry in reported])
            return bool(ping(dbapi_connection))

        monkeypatch.setattr(engine.dialect, "do_ping", _recording_ping)
        try:
            await _raw_connection(engine)  # the pool now holds an established connection, pre-pinged next
            with observing_checkouts(reported.append):
                raw = await _raw_connection(engine)
            assert raw is not None
            assert [entry.dbapi_connection for entry in reported] == [raw]
            assert known_at_ping == [[raw]]

            await _raw_connection(engine)  # outside the block nothing is reported
            assert len(reported) == 1
        finally:
            await registry.close()

    async def test_only_the_observing_tasks_checkouts_of_the_given_pool_are_reported(self, tmp_path: Path) -> None:
        registry = _sqlite_registry(tmp_path / "own.db")
        other = _sqlite_registry(tmp_path / "other.db")
        engine = registry.primary.engine
        reported: list[Any] = []
        try:
            with observing_checkouts(reported.append, pool=engine.sync_engine.pool):
                own = await _raw_connection(engine)
                # A task created inside the block copies the context variable that carries the observer.
                await asyncio.create_task(_raw_connection(engine))
                await _raw_connection(other.primary.engine)
            assert [entry.dbapi_connection for entry in reported] == [own]
        finally:
            await registry.close()
            await other.close()

    async def test_an_observer_that_raises_leaves_the_entry_in_the_pool(self, tmp_path: Path) -> None:
        registry = _sqlite_registry(tmp_path / "raising.db")
        engine = registry.primary.engine

        def _raising(_entry: Any) -> None:
            raise LookupError("observer failed")

        try:
            await _raw_connection(engine)
            with observing_checkouts(_raising), pytest.raises(LookupError):
                await _raw_connection(engine)
            assert engine.pool.checkedout() == 0
            assert await _raw_connection(engine) is not None  # the entry went back and serves the next checkout
        finally:
            await registry.close()

    async def test_a_task_started_during_the_checks_pre_ping_leaves_its_entry_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Application work started while the check pre-pings (by a pool listener, say) inherits the
        # check's context. The connection it borrows must never become the one a late check closes.
        registry = _sqlite_registry(tmp_path / "child.db", pre_ping=True)
        engine = registry.primary.engine
        loop = asyncio.get_running_loop()
        check = _Check(engine, loop.time())
        child_holds, release_child = asyncio.Event(), asyncio.Event()
        seen: dict[str, Any] = {}
        ping = engine.dialect.do_ping

        async def _child() -> None:
            async with engine.connect() as conn:
                seen["child"] = conn.sync_connection.connection.dbapi_connection
                child_holds.set()
                await release_child.wait()

        def _ping_while_a_child_checks_out(dbapi_connection: Any) -> bool:
            seen["child_task"] = loop.create_task(_child())
            await_only(child_holds.wait())  # the check waits in its pre-ping while the child checks out
            seen["pinged"] = dbapi_connection
            seen["entry"] = check.entry.dbapi_connection if check.entry is not None else None
            return bool(ping(dbapi_connection))

        monkeypatch.setattr(engine.dialect, "do_ping", _ping_while_a_child_checks_out)
        try:
            await _raw_connection(engine)  # an established connection for the check to pre-ping
            check.task = loop.create_task(_select_one(check))
            result = await check.task
            assert result.status == "UP", result.details
            assert seen["child"] is not seen["pinged"]
            assert seen["entry"] is seen["pinged"]
            assert check.entry is None
        finally:
            release_child.set()
            if "child_task" in seen:
                await seen["child_task"]
            await registry.close()

    async def test_the_entry_is_dropped_when_the_checkout_fails(self, tmp_path: Path) -> None:
        registry = _sqlite_registry(tmp_path / "refused.db")
        engine = registry.primary.engine
        loop = asyncio.get_running_loop()
        check = _Check(engine, loop.time())
        during: list[tuple[Any, Any]] = []

        def _refuse(_dbapi_connection: Any, record: Any, _proxy: Any) -> None:
            during.append((check.entry, record))
            raise RuntimeError("checkout refused")

        try:
            await _raw_connection(engine)
            event.listen(engine.sync_engine, "checkout", _refuse)
            check.task = loop.create_task(_select_one(check))
            result = await check.task
            assert result.status == "DOWN"
            assert result.details["error"] == "RuntimeError"
            assert len(during) == 1
            assert during[0][0] is during[0][1]  # the check knew the entry it was checking out
            assert check.entry is None  # and let go of it with the failed checkout
            assert engine.pool.checkedout() == 0
        finally:
            await registry.close()


class TestEveryDatasource:
    async def test_registry_datasources_are_all_checked(self, tmp_path: Path) -> None:
        registry = DataSourceRegistry.for_config(
            Config(
                {
                    "pyfly": {
                        "data": {
                            "relational": {
                                "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                                "read-replica": {"url": f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}"},
                                "datasources": {
                                    "reporting": {"url": f"sqlite+aiosqlite:///{tmp_path / 'reporting.db'}"},
                                    "legacy": {
                                        "url": "postgresql+asyncpg://bad:bad@127.0.0.1:1/nope",
                                        "connect-args": {"timeout": 1},
                                    },
                                },
                            }
                        }
                    }
                }
            )
        )
        try:
            indicator = SqlAlchemyHealthIndicator(registry.primary.engine, registry=registry, timeout=2.0)
            result = await indicator.health()
        finally:
            await registry.close()
        assert result.status == "DOWN"
        assert result.details["database"] == "sqlite"
        datasources = result.details["datasources"]
        assert set(datasources) == {"primary", "primary.replica", "reporting", "legacy"}
        assert datasources["primary"]["status"] == "UP"
        assert datasources["primary.replica"]["status"] == "UP"
        assert datasources["reporting"]["status"] == "UP"
        assert datasources["legacy"]["status"] == "DOWN"
        assert datasources["legacy"]["database"] == "postgresql"

    async def test_details_never_carry_the_password(self, silent_database: str) -> None:
        engine = create_async_engine(silent_database)
        try:
            result = await SqlAlchemyHealthIndicator(engine, timeout=0.2).health()
        finally:
            await engine.dispose()
        assert "secret" not in repr(result.details)


class TestOneCheckPerProbe:
    """A probe GET used to aggregate twice (body, then status code): two database checks, twice the
    worst-case latency, and a body and a status code that could disagree."""

    @pytest.mark.parametrize("path", ["/actuator/health", "/actuator/health/readiness", "/actuator/health/liveness"])
    def test_each_probe_runs_the_indicators_once(self, path: str) -> None:
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
        from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
        from pyfly.actuator.registry import ActuatorRegistry

        calls: list[str] = []

        class Counting:
            async def health(self) -> HealthStatus:
                calls.append("check")
                return HealthStatus(status="UP")

        aggregator = HealthAggregator()
        aggregator.add_indicator("db", Counting())
        registry = ActuatorRegistry()
        registry.register(HealthEndpoint(aggregator))
        app = Starlette(routes=make_starlette_actuator_routes(registry))
        response = TestClient(app).get(path)
        assert response.status_code == 200
        assert calls == ["check"]
