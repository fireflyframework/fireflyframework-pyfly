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
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import String as SAString
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.actuator.health import HealthAggregator, HealthStatus, ProbeGroup
from pyfly.actuator.wiring import install_health_indicators
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.auto_configuration import EngineLifecycle
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
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

    @pytest.mark.asyncio
    async def test_invalid_ddl_auto_treated_as_create(self) -> None:
        """An unknown ddl-auto value falls back to 'create' per the implementation."""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        session: AsyncSession = session_factory()

        lifecycle = EngineLifecycle(engine, session, ddl_auto="bogus_value")
        # "bogus_value" is not in _VALID_DDL_MODES so it falls back to "create"
        assert lifecycle._ddl_auto == "create"
        try:
            await lifecycle.start()

            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT 1 FROM canary_lifecycle_test LIMIT 1"))
                assert result is not None
        finally:
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: _Canary.__table__.drop(c, checkfirst=True))
            await lifecycle.stop()


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
