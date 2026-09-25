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
"""Relational data layer (SQLAlchemy) auto-configuration.

Two auto-configurations live here:

- :class:`DataSourceAutoConfiguration` exposes the application's
  :class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry` whenever SQLAlchemy is
  installed, disposes it when the context stops, and registers the after-begin customizer and
  credentials provider beans on it. Modules that store data in SQL (event store, snapshots, saga
  persistence, the PostgreSQL cache) take their datasource from it, so it exists even when the
  relational repositories are disabled.
- :class:`RelationalAutoConfiguration` (``pyfly.data.relational.enabled=true``) keeps the beans an
  application injects (``async_engine``, ``async_session_factory``, ``routing_session_factory``,
  ``named_data_sources``, ``async_session``, ``engine_lifecycle``, ``db_health_indicator``,
  ``query_metrics``) with their names and types. Each is now a view over the registry.
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import inspect
import logging
from typing import Any

try:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

    from pyfly.data.relational.datasource_registry import DataSourceRegistry, datasource_of
except ImportError:
    AsyncEngine = object  # type: ignore[misc,assignment]
    AsyncSession = object  # type: ignore[misc,assignment]
    DataSourceRegistry = object  # type: ignore[misc,assignment]

from pyfly.config.properties.data import RelationalProperties
from pyfly.container.bean import bean
from pyfly.container.types import Scope
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_property,
)
from pyfly.context.events import RefreshScopeRefreshedEvent, app_event_listener
from pyfly.core.config import Config
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from pyfly.data.relational.metrics import SqlAlchemyPoolMetrics, SqlAlchemyQueryMetrics
from pyfly.data.relational.migrations import MigrationRunner
from pyfly.data.relational.named_datasources import NamedDataSources
from pyfly.data.relational.routing import RoutingSessionFactory
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)

try:
    from pyfly.observability.metrics import MetricsRegistry
except ImportError:
    MetricsRegistry = object  # type: ignore[misc,assignment]

_logger = logging.getLogger(__name__)


class QueryMetricsLifecycle:
    """Lifecycle adapter that wires query and pool metrics on startup.

    Implements ``start()`` / ``stop()`` so the ``ApplicationContext`` auto-discovers it as an
    infrastructure adapter. ``start()`` attaches :class:`SqlAlchemyQueryMetrics` (query duration,
    count, errors) and :class:`SqlAlchemyPoolMetrics` (pool size, checked out, idle, overflow, per
    datasource) to every engine of *datasource_registry*, including the datasources a module registers
    later; without a registry, to *engine* alone. ``stop()`` is a no-op (listeners live as long as the
    engines).
    """

    def __init__(self, engine: AsyncEngine, recorder: Any, *, datasource_registry: Any = None) -> None:
        self._engine = engine
        self._recorder = recorder
        self._registry = datasource_registry
        self._metrics = SqlAlchemyQueryMetrics(engine, recorder)
        self._attached: dict[int, SqlAlchemyQueryMetrics] = {}
        self._pool_metrics = SqlAlchemyPoolMetrics(recorder)

    async def start(self) -> None:
        """Attach query and pool metrics to every datasource engine."""
        if self._registry is None:
            self._metrics.attach()
            self._pool_metrics.bind("primary", self._engine)
            return
        self._registry.on_register(self._attach)

    def _attach(self, datasource: Any) -> None:
        engine = datasource.engine
        if id(engine) not in self._attached:
            metrics = self._metrics if engine is self._engine else SqlAlchemyQueryMetrics(engine, self._recorder)
            metrics.attach()
            self._attached[id(engine)] = metrics
        self._pool_metrics.bind(datasource.qualified_name, engine)

    async def stop(self) -> None:
        """No-op — listeners are passive and need no teardown."""


class EngineLifecycle:
    """Lifecycle wrapper for the SQLAlchemy async engine.

    Implements ``start()`` / ``stop()`` so the ``ApplicationContext``
    auto-discovers it as an infrastructure adapter.

    On ``start()``, applies the ``ddl-auto`` schema strategy:

    * ``create`` — create tables that don't exist (safe, idempotent)
    * ``create-drop`` — create on start, drop on shutdown
    * ``none`` — skip DDL (for Alembic-managed databases)

    ``stop()`` closes the session it was given and, when *dispose_engine* is true (a standalone
    engine), disposes the engine. A registry engine is left to the registry, which disposes every
    engine once when the context stops.
    """

    _VALID_DDL_MODES = {"none", "create", "create-drop"}

    def __init__(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        *,
        ddl_auto: str = "create",
        dispose_engine: bool = True,
    ) -> None:
        self._engine = engine
        self._session = session
        self._ddl_auto = ddl_auto if ddl_auto in self._VALID_DDL_MODES else "create"
        self._dispose_engine = dispose_engine

    async def start(self) -> None:
        """Apply DDL strategy — create tables from Base.metadata when configured."""
        if self._ddl_auto in ("create", "create-drop"):
            from pyfly.data.relational.sqlalchemy.entity import Base

            _logger.info("Initializing database schema (ddl-auto=%s)", self._ddl_auto)
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            _logger.info("Database schema initialized (%d tables)", len(Base.metadata.tables))

    async def stop(self) -> None:
        """Drop the schema for ``create-drop``, close the shared session, dispose a standalone engine."""
        if self._ddl_auto == "create-drop":
            from pyfly.data.relational.sqlalchemy.entity import Base

            _logger.info("Dropping database schema (ddl-auto=create-drop)")
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)

        try:
            await self._session.close()
        except Exception:
            _logger.debug("session_close_failed", exc_info=True)
        if self._dispose_engine:
            await self._engine.dispose()


class DataSourceRegistryLifecycle:
    """Closes the datasource registry when the context stops, and rotates credentials on refresh.

    ``stop()`` disposes every engine of the registry exactly once. On a configuration refresh
    (``RefreshScopeRefreshedEvent``) the pools whose credentials changed are soft-evicted.
    """

    def __init__(self, registry: DataSourceRegistry) -> None:
        self._registry = registry

    async def start(self) -> None:
        """No-op: the registry builds its datasources when they are first asked for."""

    async def stop(self) -> None:
        """Dispose every engine of the registry."""
        await self._registry.close()

    @app_event_listener
    async def on_refresh(self, event: RefreshScopeRefreshedEvent) -> None:
        """Soft-evict the pools whose credentials changed with the refreshed configuration."""
        if not self._registry.closed:
            evicted = await self._registry.refresh_credentials()
            if evicted:
                _logger.info("datasource_pools_evicted_on_refresh", extra={"datasources": evicted})


def _defines_coroutine(bean_instance: object, name: str) -> bool:
    """Whether the bean's class (not a ``__getattr__`` proxy or a mock) defines coroutine *name*."""
    return inspect.iscoroutinefunction(getattr(type(bean_instance), name, None))


def _defines_method(bean_instance: object, name: str) -> bool:
    member = getattr(type(bean_instance), name, None)
    return callable(member) and not inspect.iscoroutinefunction(member)


class DataSourceSpiRegistrar:
    """``BeanPostProcessor`` that hands the datasource SPI beans to the registry.

    A bean whose class defines ``async def after_begin(connection, datasource)`` is an
    :class:`~pyfly.data.relational.dialect_customizers.AfterBeginCustomizer` (limited to the names in
    its ``datasources`` attribute, if it has one); a bean whose class defines
    ``datasource_credentials(datasource)`` is a
    :class:`~pyfly.data.relational.dialect_customizers.DataSourceCredentialsProvider`.
    """

    def __init__(self, registry: DataSourceRegistry) -> None:
        self._registry = registry

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Pass through."""
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Register *bean* with the registry when it implements one of the SPIs."""
        from pyfly.data.relational.datasource_registry import DataSource
        from pyfly.data.relational.dialect_customizers import customizer_datasources

        if isinstance(bean, DataSource):  # its after_begin() runs the customizers; it is not one
            return bean
        if _defines_coroutine(bean, "after_begin"):
            self._registry.add_scoped_customizer(bean, customizer_datasources(bean))
        if _defines_method(bean, "datasource_credentials"):
            self._registry.add_credentials_provider(bean)
        return bean


@auto_configuration
@conditional_on_class("sqlalchemy")
class DataSourceAutoConfiguration:
    """The application's datasource registry, its lifecycle, and its SPI registrar."""

    @bean
    def datasource_registry(self, config: Config) -> DataSourceRegistry:
        """The registry of this configuration (:meth:`DataSourceRegistry.for_config`).

        Every module that needs SQL resolves its datasource here; it is the same object whichever
        auto-configuration asks first.
        """
        return DataSourceRegistry.for_config(config)

    @bean
    def datasource_registry_lifecycle(self, datasource_registry: DataSourceRegistry) -> DataSourceRegistryLifecycle:
        """Disposes every engine when the context stops; soft-evicts rotated credentials on refresh."""
        return DataSourceRegistryLifecycle(datasource_registry)

    @bean
    def datasource_spi_registrar(self, datasource_registry: DataSourceRegistry) -> DataSourceSpiRegistrar:
        """Registers ``AfterBeginCustomizer`` and ``DataSourceCredentialsProvider`` beans."""
        return DataSourceSpiRegistrar(datasource_registry)


@auto_configuration
@conditional_on_class("sqlalchemy")
@conditional_on_property("pyfly.data.relational.enabled", having_value="true")
class RelationalAutoConfiguration:
    """Auto-configures the SQLAlchemy engine, sessions and repository post-processor as views over the
    :class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry`."""

    @bean
    def async_engine(self, config: Config) -> AsyncEngine:
        """The primary datasource's engine.

        Fails at startup when ``pyfly.data.relational.url`` is not configured (outside the ``dev``
        profile), instead of silently opening ``./app.db`` in the working directory.
        """
        return DataSourceRegistry.for_config(config).primary.engine

    @bean
    def async_session_factory(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        """The primary datasource's ``async_sessionmaker`` (``expire_on_commit=False``)."""
        datasource = datasource_of(async_engine)
        if datasource is not None:
            return datasource.sessionmaker
        return async_sessionmaker(async_engine, expire_on_commit=False)

    @bean
    def named_data_sources(self, config: Config) -> NamedDataSources:
        """Secondary datasources from ``pyfly.data.relational.datasources.<name>``.

        Inject and call ``.get("<name>")`` for that datasource's ``async_sessionmaker``. A live view
        over the registry: it also lists the datasources a module registers.
        """
        return NamedDataSources.of_registry(DataSourceRegistry.for_config(config))

    @bean
    def routing_session_factory(
        self, async_session_factory: async_sessionmaker[AsyncSession], config: Config
    ) -> RoutingSessionFactory:
        """Read/write routing session factory — the ``AbstractRoutingDataSource`` equivalent.

        Routes to the primary's read replica inside a :func:`~pyfly.data.relational.routing.read_only`
        block when ``pyfly.data.relational.read-replica.url`` is configured; otherwise it always uses
        the primary (no behavior change).
        """
        datasource = datasource_of(async_session_factory)
        if datasource is not None:
            replica = datasource.replica
        else:
            # A session factory the application declared itself still routes to the configured replica.
            registry = DataSourceRegistry.for_config(config)
            replica = registry.primary.replica if registry.has_primary else None
        return RoutingSessionFactory(async_session_factory, replica.sessionmaker if replica is not None else None)

    @bean(scope=Scope.TRANSIENT)
    def async_session(self, async_session_factory: async_sessionmaker[AsyncSession]) -> AsyncSession:
        """Create an ``AsyncSession`` from the factory — a NEW one for every injection.

        This bean was a singleton until 26.09.06, so every repository, every user bean and the
        engine lifecycle shared one SQLAlchemy session: one transaction, one identity map and one
        connection's local state (``SET LOCAL``, a tenant GUC, a ``search_path``) for the whole
        process. Anything multi-tenant or concurrent had to refuse the bean and open its own
        sessions by hand. The factory (``async_session_factory``) is the unit of sharing; the
        session is the unit of work, so it is transient: a bean that injects ``AsyncSession``
        owns the one it receives, and a bean that needs a session per request or per tenant
        injects the factory and calls it.

        The one session ``engine_lifecycle`` receives is the one it closes at shutdown.
        """
        session: AsyncSession = async_session_factory()
        return session

    @bean
    def engine_lifecycle(
        self, async_engine: AsyncEngine, async_session: AsyncSession, config: Config
    ) -> EngineLifecycle:
        """Lifecycle bean — creates tables on startup based on ``ddl-auto`` config.

        The engine itself is disposed by the registry, after this lifecycle has stopped.
        """
        datasource = datasource_of(async_engine)
        registry = datasource.registry if datasource is not None else None
        ddl_auto = (
            registry.properties.ddl_auto
            if registry is not None
            else str(config.get("pyfly.data.relational.ddl-auto", "create"))
        )
        return EngineLifecycle(async_engine, async_session, ddl_auto=ddl_auto, dispose_engine=registry is None)

    @bean
    def repository_post_processor(self) -> RepositoryBeanPostProcessor:
        return RepositoryBeanPostProcessor()

    @bean
    def db_health_indicator(self, async_engine: AsyncEngine) -> SqlAlchemyHealthIndicator:
        """Database ``HealthIndicator`` — contributed to ``/actuator/health`` and to the readiness probe.

        It checks every registry datasource (primary, replicas, named, module datasources) with a
        timeout (``pyfly.data.relational.health.timeout``, 2 s by default), and it belongs to the
        readiness group only: a database blip takes the pod out of the load balancer instead of
        restarting it.
        """
        datasource = datasource_of(async_engine)
        registry = datasource.registry if datasource is not None else None
        timeout = registry.properties.health.timeout if registry is not None else 2.0
        return SqlAlchemyHealthIndicator(async_engine, registry=registry, timeout=timeout)

    @bean
    def auditing_entity_listener(self) -> AuditingEntityListener:
        """Registers SQLAlchemy ORM events for automatic audit field population."""
        listener = AuditingEntityListener()
        listener.register()
        return listener

    @bean
    def query_metrics(
        self, async_engine: AsyncEngine, registry: MetricsRegistry | None = None
    ) -> QueryMetricsLifecycle | None:
        """Lifecycle bean that attaches query and pool metrics to every datasource engine.

        Created only when a :class:`~pyfly.observability.metrics.MetricsRegistry` bean
        is present (i.e. when ``prometheus_client`` is installed and the observability
        auto-configuration is active).  When the registry is absent the relational
        module continues to work unchanged — this bean simply returns ``None`` and is
        skipped by the context lifecycle machinery.
        """
        if registry is None:
            return None
        datasource = datasource_of(async_engine)
        return QueryMetricsLifecycle(
            async_engine, registry, datasource_registry=datasource.registry if datasource is not None else None
        )


@auto_configuration
@conditional_on_property("pyfly.data.relational.migrations.enabled", having_value="true")
class MigrationAutoConfiguration:
    """Applies Alembic migrations on startup (Spring Boot Flyway-style auto-migrate).

    Opt-in via ``pyfly.data.relational.migrations.enabled=true``; reuses the project's
    Alembic environment (``pyfly db init``). Migrates the same datasource as the app.
    """

    @bean
    def migration_runner(self, config: Config) -> MigrationRunner:
        # The same URL the primary engine uses (the legacy pyfly.data.url alias included).
        return MigrationRunner(
            url=RelationalProperties.from_config(config).url or "",
            config_path=str(config.get("pyfly.data.relational.migrations.config", "alembic.ini")),
            revision=str(config.get("pyfly.data.relational.migrations.revision", "head")),
        )
