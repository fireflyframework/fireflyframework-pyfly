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
"""Relational data layer (SQLAlchemy) auto-configuration."""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import logging

try:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
except ImportError:
    AsyncEngine = object  # type: ignore[misc,assignment]
    AsyncSession = object  # type: ignore[misc,assignment]

from pyfly.container.bean import bean
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)

_logger = logging.getLogger(__name__)


class EngineLifecycle:
    """Lifecycle wrapper for the SQLAlchemy async engine.

    Implements ``start()`` / ``stop()`` so the ``ApplicationContext``
    auto-discovers it as an infrastructure adapter.

    On ``start()``, applies the ``ddl-auto`` schema strategy:

    * ``create`` — create tables that don't exist (safe, idempotent)
    * ``create-drop`` — create on start, drop on shutdown
    * ``none`` — skip DDL (for Alembic-managed databases)
    """

    _VALID_DDL_MODES = {"none", "create", "create-drop"}

    def __init__(self, engine: AsyncEngine, session: AsyncSession, *, ddl_auto: str = "create") -> None:
        self._engine = engine
        self._session = session
        self._ddl_auto = ddl_auto if ddl_auto in self._VALID_DDL_MODES else "create"

    async def start(self) -> None:
        """Apply DDL strategy — create tables from Base.metadata when configured."""
        if self._ddl_auto in ("create", "create-drop"):
            from pyfly.data.relational.sqlalchemy.entity import Base

            _logger.info("Initializing database schema (ddl-auto=%s)", self._ddl_auto)
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            _logger.info("Database schema initialized (%d tables)", len(Base.metadata.tables))

    async def stop(self) -> None:
        """Dispose engine connection pool and close the shared session."""
        if self._ddl_auto == "create-drop":
            from pyfly.data.relational.sqlalchemy.entity import Base

            _logger.info("Dropping database schema (ddl-auto=create-drop)")
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)

        try:
            await self._session.close()
        except Exception:
            _logger.debug("session_close_failed", exc_info=True)
        await self._engine.dispose()


@auto_configuration
@conditional_on_class("sqlalchemy")
@conditional_on_property("pyfly.data.relational.enabled", having_value="true")
class RelationalAutoConfiguration:
    """Auto-configures SQLAlchemy engine, session, and repository post-processor."""

    @bean
    def async_engine(self, config: Config) -> AsyncEngine:
        url = str(config.get("pyfly.data.relational.url", "sqlite+aiosqlite:///./app.db"))
        echo = bool(config.get("pyfly.data.relational.echo", False))
        return create_async_engine(url, echo=echo)

    @bean
    def async_session_factory(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        """Create an ``async_sessionmaker`` factory bound to the engine."""
        return async_sessionmaker(async_engine, expire_on_commit=False)

    @bean
    def async_session(self, async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
        """Create an ``AsyncSession`` from the factory.

        .. warning::
            This bean returns a **single session instance** shared across
            injections.  In production you should manage session lifecycle
            per-request (e.g. via middleware or a request-scoped provider).
        """
        session: AsyncSession = async_session_factory()
        return session

    @bean
    def engine_lifecycle(
        self, async_engine: AsyncEngine, async_session: AsyncSession, config: Config
    ) -> EngineLifecycle:
        """Lifecycle bean — creates tables on startup based on ``ddl-auto`` config."""
        ddl_auto = str(config.get("pyfly.data.relational.ddl-auto", "create"))
        return EngineLifecycle(async_engine, async_session, ddl_auto=ddl_auto)

    @bean
    def repository_post_processor(self) -> RepositoryBeanPostProcessor:
        return RepositoryBeanPostProcessor()

    @bean
    def auditing_entity_listener(self) -> AuditingEntityListener:
        """Registers SQLAlchemy ORM events for automatic audit field population."""
        listener = AuditingEntityListener()
        listener.register()
        return listener
