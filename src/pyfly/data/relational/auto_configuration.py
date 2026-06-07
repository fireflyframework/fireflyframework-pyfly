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
from typing import Any

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
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from pyfly.data.relational.named_datasources import NamedDataSources, build_named_data_sources
from pyfly.data.relational.routing import RoutingSessionFactory
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

        # Forward connection-pool tuning to create_async_engine (audit #107) —
        # only the keys that were explicitly configured (SQLite's default pool
        # rejects sizing kwargs).
        pool_kwargs: dict[str, Any] = {}
        for key, engine_arg, caster in (
            ("pool.size", "pool_size", int),
            ("pool.max-overflow", "max_overflow", int),
            ("pool.timeout", "pool_timeout", float),
            ("pool.recycle", "pool_recycle", int),
        ):
            value = config.get(f"pyfly.data.relational.{key}")
            if value is not None:
                pool_kwargs[engine_arg] = caster(value)
        pre_ping = config.get("pyfly.data.relational.pool.pre-ping")
        if pre_ping is not None:
            pool_kwargs["pool_pre_ping"] = str(pre_ping).lower() in ("true", "1", "yes")

        return create_async_engine(url, echo=echo, **pool_kwargs)

    @bean
    def async_session_factory(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        """Create an ``async_sessionmaker`` factory bound to the engine."""
        return async_sessionmaker(async_engine, expire_on_commit=False)

    @bean
    def named_data_sources(self, config: Config) -> NamedDataSources:
        """Secondary datasources from ``pyfly.data.relational.datasources.<name>``.

        Inject and call ``.get("<name>")`` for that datasource's ``async_sessionmaker``.
        Empty when none are configured (the primary keeps its dedicated beans).
        """
        return build_named_data_sources(
            config,
            lambda url, echo=False: create_async_engine(url, echo=echo),
            lambda engine: async_sessionmaker(engine, expire_on_commit=False),
        )

    @bean
    def routing_session_factory(
        self, async_session_factory: async_sessionmaker[AsyncSession], config: Config
    ) -> RoutingSessionFactory:
        """Read/write routing session factory — the ``AbstractRoutingDataSource`` equivalent.

        Routes to a read-replica inside a :func:`~pyfly.data.relational.routing.read_only`
        block when ``pyfly.data.relational.read-replica.url`` is configured; otherwise it
        always uses the primary (no behavior change).
        """
        replica_url = config.get("pyfly.data.relational.read-replica.url")
        replica_factory: async_sessionmaker[AsyncSession] | None = None
        if replica_url:
            echo = bool(config.get("pyfly.data.relational.echo", False))
            replica_engine = create_async_engine(str(replica_url), echo=echo)
            replica_factory = async_sessionmaker(replica_engine, expire_on_commit=False)
        return RoutingSessionFactory(async_session_factory, replica_factory)

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
    def db_health_indicator(self, async_engine: AsyncEngine) -> SqlAlchemyHealthIndicator:
        """Database ``HealthIndicator`` — auto-discovered by the actuator and
        contributed to ``/actuator/health`` as the ``db`` component."""
        return SqlAlchemyHealthIndicator(async_engine)

    @bean
    def auditing_entity_listener(self) -> AuditingEntityListener:
        """Registers SQLAlchemy ORM events for automatic audit field population."""
        listener = AuditingEntityListener()
        listener.register()
        return listener
