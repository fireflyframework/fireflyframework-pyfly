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
"""Unit tests for SqlAlchemyHealthIndicator and EngineLifecycle.

No Docker required — all tests use in-memory SQLite via aiosqlite or
an unreachable/invalid URL to exercise the DOWN path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import String as SAString
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.actuator.health import HealthStatus
from pyfly.data.relational.auto_configuration import EngineLifecycle
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from pyfly.data.relational.sqlalchemy.entity import BaseEntity

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
