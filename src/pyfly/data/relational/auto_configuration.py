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
  relational repositories are disabled. It also exposes the
  :class:`~pyfly.data.transaction.registry.TransactionManagerRegistry` (one SQLAlchemy transaction manager
  per datasource), installed while the context runs, so ``@transactional`` and repositories find their
  transaction manager by datasource name, and the :class:`RepositoryWiringCheck`, which fails the start on a
  relational repository whose derived or ``@query`` stubs no repository post-processor compiled.
- :class:`RelationalAutoConfiguration` (``pyfly.data.relational.enabled=true``) keeps the beans an
  application injects (``async_engine``, ``async_session_factory``, ``routing_session_factory``,
  ``named_data_sources``, ``async_session``, ``engine_lifecycle``, ``db_health_indicator``,
  ``query_metrics``) with their names and types. Each is now a view over the registry (the
  ``DataSourceRegistry`` bean); ``engine_lifecycle`` applies the schema strategy (``ddl-auto``) through a
  :class:`~pyfly.data.relational.schema.SchemaInitializer`. It adds the ``session_provider`` bean, and the
  ``primary_transaction_manager`` bean, which serves the ``primary`` datasource's units on the primary
  ``async_sessionmaker`` bean: an application's singleton session factory, engine or registry bean replaces
  the primary for ``@transactional``, repositories, ``SessionProvider``, the ``AsyncSession`` bean and
  ``infrastructure_unit()`` alike, and ``primary_transaction_manager_binding`` undoes that binding when the
  context stops (contexts started on one ``Config`` share its registry and transaction managers).
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import inspect
import logging
import weakref
from collections.abc import Callable
from typing import Any

try:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
    )

    from pyfly.data.relational.datasource_registry import PRIMARY, DataSourceRegistry, datasource_of
    from pyfly.data.relational.schema import SCHEMA_PHASE, SchemaInitializer
    from pyfly.data.relational.sqlalchemy.repository import Repository
    from pyfly.data.relational.sqlalchemy.session import ScopedAsyncSession, SessionProvider
    from pyfly.data.relational.sqlalchemy.transaction_manager import (
        SqlAlchemyTransactionManager,
        bind_primary_session_factory,
        transaction_managers_for,
        unbind_primary_session_factory,
    )
except ImportError:
    AsyncEngine = object  # type: ignore[misc,assignment]
    AsyncSession = object  # type: ignore[misc,assignment]
    DataSourceRegistry = object  # type: ignore[misc,assignment]
    SCHEMA_PHASE = 0
    SchemaInitializer = object  # type: ignore[misc,assignment]
    Repository = object  # type: ignore[misc,assignment]
    SessionProvider = object  # type: ignore[misc,assignment]
    SqlAlchemyTransactionManager = object  # type: ignore[misc,assignment]

from pyfly.config.properties.data import RelationalProperties
from pyfly.container.bean import bean
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError, NoUniqueBeanError
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.container.provider import Provider
from pyfly.container.types import Scope, ScopeSpec, scope_name
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.context.events import RefreshScopeRefreshedEvent, app_event_listener
from pyfly.core.config import Config
from pyfly.data.auditing import AuditorAware, DateTimeProvider
from pyfly.data.relational.health import SqlAlchemyHealthIndicator
from pyfly.data.relational.metrics import SqlAlchemyPoolMetrics, SqlAlchemyQueryMetrics
from pyfly.data.relational.migrations import MigrationRunner
from pyfly.data.relational.named_datasources import NamedDataSources
from pyfly.data.relational.routing import RoutingSessionFactory
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
from pyfly.data.relational.sqlalchemy.post_processor import (
    RepositoryBeanPostProcessor,
)
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.registry import TransactionManagerRegistry, install_registry, uninstall_registry

try:
    from pyfly.observability.metrics import MetricsRegistry
except ImportError:
    MetricsRegistry = object  # type: ignore[misc,assignment]

try:
    from pyfly.data.relational.datasource_registry import close_connections_on_return
except ImportError:  # without SQLAlchemy there is no engine, and nothing calls it

    def close_connections_on_return(engine: Any) -> None:  # type: ignore[misc]
        """Nothing to hook: SQLAlchemy is not installed."""


_logger = logging.getLogger(__name__)

# The engines a split primary was reported for, per registry: a restarted context (it builds a new registry)
# warns again.
_SPLIT_REPORTED: weakref.WeakKeyDictionary[Any, weakref.WeakSet[Any]] = weakref.WeakKeyDictionary()

_ENGINE_SPLIT_HINT = (
    "@transactional, repositories, SessionProvider, infrastructure_unit() and the AsyncSession bean run on "
    "this engine, the application's primary, while DataSourceRegistry.primary keeps pyfly.data.relational.url "
    "for the modules that look the registry up (event store, snapshots, saga persistence, the PostgreSQL cache) "
    "and for health and pool metrics; configure the primary under pyfly.data.relational (url, connect-args, "
    "pool) and a second database under pyfly.data.relational.datasources.<name> instead of declaring an engine "
    "or session factory bean, or leave pyfly.data.relational.url unset when this engine is the only primary"
)

_NAMED_SPLIT_HINT = (
    "the primary session factory is bound to another datasource of the registry: @transactional, repositories, "
    "SessionProvider, infrastructure_unit() and the AsyncSession bean run their primary units on its database, "
    "apart from the units that name that datasource, and DataSourceRegistry.primary keeps "
    "pyfly.data.relational.url; configure that database as the primary under pyfly.data.relational, or name "
    "the datasource where it is used (@transactional(datasource=...), __datasource__, "
    "registry.session_factory(name)) instead of declaring a session factory bean over it"
)


_REGISTRY_SPLIT_HINT = (
    "@transactional, repositories, SessionProvider, infrastructure_unit() and the relational beans run on this "
    "registry, the application's DataSourceRegistry bean, while the modules that look the registry up by "
    "configuration (event store, snapshots, saga persistence, the PostgreSQL cache: "
    "DataSourceRegistry.for_config) keep the configuration's, with engines and pools of their own built from "
    "pyfly.data.relational; declare the datasources under pyfly.data.relational instead of a registry bean, or "
    "return DataSourceRegistry.for_config(config) from it"
)

# The application registries a split from the configuration's was reported for: a restarted context (it builds
# the bean again) warns again.
_REGISTRY_SPLIT_REPORTED: weakref.WeakSet[Any] = weakref.WeakSet()


def _warn_if_registry_split(datasource_registry: DataSourceRegistry, config: Config) -> None:
    """WARNING (``relational_registry_not_the_configurations``), once per registry, when the context's
    ``DataSourceRegistry`` bean is the application's own while ``pyfly.data.relational.url`` is configured:
    the units of work run on it, and the modules that look the registry up by configuration on the
    configuration's (the counterpart of ``relational_engine_not_in_registry`` for a registry bean)."""
    if datasource_registry in _REGISTRY_SPLIT_REPORTED:
        return
    if not str(RelationalProperties.from_config(config).url or "").strip():
        return
    if datasource_registry is DataSourceRegistry.for_config(config):
        return
    _REGISTRY_SPLIT_REPORTED.add(datasource_registry)
    _logger.warning("relational_registry_not_the_configurations", extra={"hint": _REGISTRY_SPLIT_HINT})


def _datasources(datasource_registry: DataSourceRegistry | None, config: Config) -> DataSourceRegistry:
    """The context's ``DataSourceRegistry`` bean (an application's singleton one replaces the configuration's).

    This auto-configuration can be processed before ``DataSourceAutoConfiguration``; without an application
    registry the bean is not registered yet then, and it will be the configuration's registry, returned here.
    """
    return datasource_registry if datasource_registry is not None else DataSourceRegistry.for_config(config)


def _warn_if_split_primary(manager: Any, registry: DataSourceRegistry) -> None:
    """WARNING when the primary's units (on *manager*, the primary session factory's) still split from a
    datasource of *registry*; once per engine and registry.

    - An engine the registry did not build (an ``AsyncEngine`` bean, a session factory bean over an engine of
      its own), while ``pyfly.data.relational.url`` is configured: ``relational_engine_not_in_registry``.
      Every unit of work runs on that engine, and ``DataSourceRegistry.primary``, for the modules that look
      the registry up, on the URL.
    - A session factory over a named datasource or a replica of the registry:
      ``relational_primary_on_named_datasource``. The ``primary`` units and that datasource's own units are
      two units on one database.

    A session factory over the registry's primary engine (other session options) is no split.
    """
    owner = manager.data_source
    if owner is not None and owner.name == PRIMARY and not owner.is_replica:
        return
    if owner is None and not str(registry.properties.url or "").strip():
        return  # the application's engine is the only primary
    try:
        engine = manager.engine
    except IllegalTransactionStateError:
        return  # a session factory bound to no engine: nothing to compare
    reported = _SPLIT_REPORTED.setdefault(registry, weakref.WeakSet())
    if engine in reported:
        return
    reported.add(engine)
    if owner is None:
        _logger.warning(
            "relational_engine_not_in_registry", extra={"engine": str(engine.url), "hint": _ENGINE_SPLIT_HINT}
        )
    else:
        _logger.warning(
            "relational_primary_on_named_datasource",
            extra={"engine": str(engine.url), "datasource": owner.qualified_name, "hint": _NAMED_SPLIT_HINT},
        )


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

    The schema strategy (``ddl-auto``) is its :class:`~pyfly.data.relational.schema.SchemaInitializer`
    (*schema*, or one built for *engine* and *ddl_auto*): ``start()`` validates the schema or creates the
    missing tables, under the schema lock that lets one instance at a time change it:

    * ``create`` — create tables that don't exist (existing tables are never altered)
    * ``create-drop`` — create on start, drop on shutdown
    * ``validate`` — fail the start when a table or column of the models is missing
    * ``none`` — skip DDL (for Alembic-managed databases)

    Without *ddl_auto* the strategy is ``create`` on an embedded database (SQLite) and ``none`` on a
    database server; an unknown value raises ``ValueError``. The lifecycle starts in
    :data:`~pyfly.data.relational.schema.SCHEMA_PHASE`, right after the startup migrations and before every
    other lifecycle bean, and stops after all of them.

    ``stop()`` closes the session it was given, drops the schema for ``create-drop`` (bounded by the drop
    timeout, logged when it fails) and, when *dispose_engine* is true (a standalone engine), disposes the
    engine even when the drop failed; a connection still in use is closed when it is returned (the hook is
    installed when the lifecycle is built). A registry engine is left to the registry, which disposes every
    engine once when the context stops.
    """

    #: Right after the startup migrations; stopped after every other lifecycle bean.
    phase = SCHEMA_PHASE

    def __init__(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        *,
        ddl_auto: str | None = None,
        dispose_engine: bool = True,
        schema: SchemaInitializer | None = None,
    ) -> None:
        self._engine = engine
        self._session = session
        self._schema = schema if schema is not None else SchemaInitializer(engine, ddl_auto=ddl_auto)
        self._dispose_engine = dispose_engine
        if dispose_engine:
            # Now, not at stop: installed then, it added pool listeners while a connect could be running.
            close_connections_on_return(engine)

    @property
    def ddl_auto(self) -> str:
        """The effective schema strategy: ``none``, ``validate``, ``create`` or ``create-drop``."""
        return self._schema.ddl_auto

    @property
    def schema(self) -> SchemaInitializer:
        """The schema strategy this lifecycle applies."""
        return self._schema

    async def start(self) -> None:
        """Apply the schema strategy."""
        await self._schema.start()

    async def stop(self) -> None:
        """Close the shared session, drop the schema for ``create-drop``, dispose a standalone engine."""
        try:
            # First: a connection the session still held would keep locks the drop waits for.
            await self._session.close()
        except Exception:
            _logger.debug("session_close_failed", exc_info=True)
        try:
            await self._schema.stop()
        finally:
            if self._dispose_engine:
                # A connection still in use (a probe in flight) is closed when it is returned: the hook was
                # installed when this lifecycle was built.
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


class TransactionManagerRegistryLifecycle:
    """Installs the context's transaction managers while the context runs.

    ``start()`` installs the registry, so a boundary that names no manager (``@transactional`` on a plain
    function or on a service without a factory attribute) and a repository outside a unit find their
    datasource's manager; ``stop()`` removes it, only if it is still the installed one (a context started
    later may have replaced it). *metrics* receives the ``pyfly.tx.synchronization.failures`` counter.
    """

    def __init__(self, registry: TransactionManagerRegistry, metrics: Any = None) -> None:
        self._registry = registry
        self._metrics = metrics

    async def start(self) -> None:
        """Install the registry (with the metrics recorder, when there is one)."""
        if self._metrics is not None:
            self._registry.metrics = self._metrics
        install_registry(self._registry)

    async def stop(self) -> None:
        """Remove the registry, if it is still the installed one."""
        uninstall_registry(self._registry)


class PrimaryTransactionManagerBinding:
    """Unbinds the primary session factory from the transaction managers when the context stops.

    Contexts started on one ``Config`` share its registry and its transaction managers; a context that stops
    must not leave its session factory (and the engine under it, which may be disposed) serving the others'
    ``primary`` units (:func:`~pyfly.data.relational.sqlalchemy.transaction_manager.unbind_primary_session_factory`,
    a no-op when another context bound its own since). The binding itself is made when the
    ``primary_transaction_manager`` bean is built, at every start.

    It is in the lowest phase, so it stops after every other lifecycle bean: one an application's
    ``@configuration`` produces starts before the auto-configured beans and stops after them, and what it
    writes in its ``stop()`` (an outbox relay, a buffered writer) must still reach the application's primary.
    """

    #: The lowest phase: started first (``start()`` does nothing) and stopped last.
    phase = -(1 << 30)

    def __init__(self, registry: DataSourceRegistry, manager: SqlAlchemyTransactionManager) -> None:
        self._registry = registry
        self._manager = manager

    async def start(self) -> None:
        """No-op: ``primary_transaction_manager`` bound the manager when it was built."""

    async def stop(self) -> None:
        """Unbind the manager, if it is still the bound one."""
        unbind_primary_session_factory(self._registry, self._manager)


def _context_transaction_managers(
    provider: Provider[TransactionManagerRegistry] | None, config: Config | None
) -> Callable[[], TransactionManagerRegistry | None]:
    """The context's ``transaction_manager_registry`` bean, looked up when first needed.

    Auto-configurations are processed in registration order and never deferred, so the bean may not be
    registered yet when a bean of :class:`RelationalAutoConfiguration` is built. Looked up later, it is the
    registry ``@transactional`` runs on, including over an application's own ``DataSourceRegistry`` bean;
    without one, the configuration's registry.
    """

    def _resolve() -> TransactionManagerRegistry | None:
        if provider is not None:
            try:
                return provider.get()
            except (NoSuchBeanError, NoUniqueBeanError):
                pass
        return transaction_managers_for(DataSourceRegistry.for_config(config)) if config is not None else None

    return _resolve


def _defines_coroutine(bean_instance: object, name: str, arity: int) -> bool:
    """Whether the bean's class (not a ``__getattr__`` proxy or a mock) defines coroutine *name*,
    callable with *arity* positional arguments."""
    member = getattr(type(bean_instance), name, None)
    return inspect.iscoroutinefunction(member) and _accepts(bean_instance, name, arity)


def _defines_method(bean_instance: object, name: str, arity: int) -> bool:
    """Whether the bean's class defines plain method *name*, callable with *arity* positional arguments."""
    member = getattr(type(bean_instance), name, None)
    return callable(member) and not inspect.iscoroutinefunction(member) and _accepts(bean_instance, name, arity)


def _accepts(bean_instance: object, name: str, arity: int) -> bool:
    try:
        inspect.signature(getattr(bean_instance, name)).bind(*([None] * arity))
    except (TypeError, ValueError):
        return False
    return True


class DataSourceSpiRegistrar:
    """``BeanPostProcessor`` that hands the datasource SPI beans to the registry.

    A bean whose class defines ``async def after_begin(connection, datasource)`` is an
    :class:`~pyfly.data.relational.dialect_customizers.AfterBeginCustomizer` (limited to the names in
    its ``datasources`` attribute, if it has one); a bean whose class defines
    ``datasource_credentials(datasource)`` is a
    :class:`~pyfly.data.relational.dialect_customizers.DataSourceCredentialsProvider`. A method of the
    same name that is not a coroutine (respectively, is one) or that cannot be called with those
    arguments is not the SPI, and the bean is left alone.

    Only singletons are registered (``singletons_only``): the registry keeps what it is given for its
    whole life, so a TRANSIENT, REQUEST or refresh-scoped SPI bean would be registered again at each
    creation and consulted after its scope ended (a request's tenant customizer running in the next
    request's units of work, an evicted credentials provider answering with the old password). Such a
    bean is ignored with a warning; declare it a singleton that reads the request or the live
    configuration when it is called.
    """

    #: The context hands this post-processor singletons only (see ``BeanPostProcessor``).
    singletons_only = True

    def __init__(self, registry: DataSourceRegistry) -> None:
        self._registry = registry
        self._warned: set[type] = set()

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Pass through."""
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Register *bean* with the registry when it implements one of the SPIs."""
        from pyfly.data.relational.dialect_customizers import customizer_datasources

        if _defines_coroutine(bean, "after_begin", 2):
            self._registry.add_scoped_customizer(bean, customizer_datasources(bean))
        if _defines_method(bean, "datasource_credentials", 1):
            self._registry.add_credentials_provider(bean)
        return bean

    def non_singleton_skipped(self, bean: Any, bean_name: str, scope: ScopeSpec) -> None:
        """Warn, once per class, that a non-singleton SPI bean is not registered."""
        cls = type(bean)
        if cls in self._warned:
            return
        if _defines_coroutine(bean, "after_begin", 2) or _defines_method(bean, "datasource_credentials", 1):
            self._warned.add(cls)
            _logger.warning(
                "datasource_spi_bean_not_singleton",
                extra={
                    "bean": cls.__qualname__,
                    "bean_name": bean_name,
                    "scope": scope_name(scope).lower(),
                    "hint": "only a singleton customizer or credentials provider is registered; "
                    "make it a singleton that reads the request or the live configuration when called",
                },
            )


@order(HIGHEST_PRECEDENCE + 200)
class RepositoryWiringCheck:
    """``BeanPostProcessor`` that fails the start on a relational repository whose query stubs were never compiled.

    The repository post-processor (the one of :class:`RelationalAutoConfiguration`, with
    ``pyfly.data.relational.enabled``, or one registered by hand) replaces a repository's derived
    (``find_by_*``, ``count_by_*``, ``exists_by_*``, ``delete_by_*``) and ``@query`` stubs with compiled queries.
    Without it the stubs keep their ``...`` bodies and answer ``None`` (``exists_by_*`` a falsy ``None``), with or
    without a database. The inherited CRUD methods need no compiling: a repository no post-processor bound to the
    context's transaction managers finds them when it is called. A repository that got a session of its own
    (manual mode) is left alone. It runs right after the repository post-processors, before the AOP one.
    """

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Pass through."""
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Refuse *bean* when it is a relational repository with a derived or ``@query`` stub no post-processor
        compiled."""
        if isinstance(bean, Repository) and bean._manual_session is None:
            stubs = _uncompiled_query_methods(bean)
            if stubs:
                raise BeanCreationException(
                    subsystem="data",
                    provider=type(bean).__name__,
                    reason=(
                        f"{type(bean).__name__} ({bean_name}) is a relational repository whose query methods "
                        f"({', '.join(stubs)}) no repository post-processor compiled: they would answer None "
                        "without touching the database. The relational data layer is not enabled: set "
                        "pyfly.data.relational.enabled=true (with pyfly.data.relational.url)."
                    ),
                )
        return bean


def _uncompiled_query_methods(bean: Any) -> list[str]:
    """The derived and ``@query`` stubs of *bean*'s class that no repository post-processor replaced on *bean*.

    It looks where the post-processor does (``BaseRepositoryPostProcessor.after_init``): the public methods the
    repository's class defines, a compiled one being bound on the instance."""
    from pyfly.data.post_processor import DERIVED_PREFIXES, BaseRepositoryPostProcessor

    cls = type(bean)
    inherited = set(dir(Repository))
    compiled = vars(bean)
    stubs: list[str] = []
    for name in vars(cls):
        if name.startswith("_") or name in compiled:
            continue
        method = getattr(cls, name, None)
        if method is None or not callable(method):
            continue
        if hasattr(method, "__pyfly_query__") or (
            name not in inherited and name.startswith(DERIVED_PREFIXES) and BaseRepositoryPostProcessor._is_stub(method)
        ):
            stubs.append(name)
    return stubs


@auto_configuration
@conditional_on_class("sqlalchemy")
class DataSourceAutoConfiguration:
    """The application's datasource registry, its lifecycle, and its SPI registrar."""

    @bean(primary=True)
    @conditional_on_missing_bean(DataSourceRegistry, singletons_only=True)
    def datasource_registry(self, config: Config) -> DataSourceRegistry:
        """The registry of this configuration (:meth:`DataSourceRegistry.for_config`).

        Every module that needs SQL resolves its datasource here; it is the same object whichever
        auto-configuration asks first. A singleton ``DataSourceRegistry`` bean of the application
        replaces it; a scoped one does not, and this one stays the ``@primary`` candidate.
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

    @bean
    def repository_wiring_check(self) -> RepositoryWiringCheck:
        """Fails the start on a relational repository whose query stubs were never compiled
        (:class:`RepositoryWiringCheck`)."""
        return RepositoryWiringCheck()

    @bean
    def transaction_manager_registry(
        self, datasource_registry: DataSourceRegistry, config: Config
    ) -> TransactionManagerRegistry:
        """One ``SqlAlchemyTransactionManager`` per datasource of the registry, by datasource name (the
        default is the primary); datasources registered later get theirs on first use. With the relational
        beans enabled, the primary's is ``primary_transaction_manager``, on the primary session factory
        bean (an application's session factory, engine or registry bean included).

        On an application's own ``DataSourceRegistry`` bean beside a configured ``pyfly.data.relational.url``
        it logs ``relational_registry_not_the_configurations``: the modules that look the registry up by
        configuration keep the configuration's."""
        _warn_if_registry_split(datasource_registry, config)
        return transaction_managers_for(datasource_registry)

    @bean
    def transaction_manager_registry_lifecycle(
        self, transaction_manager_registry: TransactionManagerRegistry, metrics: MetricsRegistry | None = None
    ) -> TransactionManagerRegistryLifecycle:
        """Installs the transaction managers while the context runs (:class:`TransactionManagerRegistryLifecycle`)."""
        return TransactionManagerRegistryLifecycle(transaction_manager_registry, metrics)


@auto_configuration
@conditional_on_class("sqlalchemy")
@conditional_on_property("pyfly.data.relational.enabled", having_value="true")
class RelationalAutoConfiguration:
    """Auto-configures the SQLAlchemy engine, sessions and repository post-processor as views over the
    :class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry` bean."""

    # The primary data beans back off for a SINGLETON of their type only, and are the @primary
    # candidates of their type. A singleton AsyncEngine, async_sessionmaker or DataSourceRegistry bean
    # replaces the application's primary everywhere (primary_transaction_manager binds the units of
    # work to it); a request- or refresh-scoped one is a second database beside it. Counting a scoped
    # one switched the primary off: start() failed (a request-scoped factory has no request at startup,
    # two scoped factories are ambiguous) or every session, the routing factory and each repository
    # moved to the scoped database.

    @bean(primary=True)
    @conditional_on_missing_bean(AsyncEngine, singletons_only=True)
    def async_engine(self, config: Config, datasource_registry: DataSourceRegistry | None = None) -> AsyncEngine:
        """The primary datasource's engine (of the ``DataSourceRegistry`` bean, an application's included).

        Fails at startup when ``pyfly.data.relational.url`` is not configured (outside the ``dev``
        profile), instead of silently opening ``./app.db`` in the working directory.
        """
        return _datasources(datasource_registry, config).primary.engine

    @bean(primary=True)
    @conditional_on_missing_bean(async_sessionmaker, singletons_only=True)
    def async_session_factory(self, async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        """The primary datasource's ``async_sessionmaker`` (``expire_on_commit=False``).

        An ``AsyncEngine`` bean of the application that the registry does not own gets a session
        factory of its own; the units of work run on it too (``primary_transaction_manager``).
        """
        datasource = datasource_of(async_engine)
        if datasource is not None:
            return datasource.sessionmaker
        return async_sessionmaker(async_engine, expire_on_commit=False)

    @bean
    def named_data_sources(
        self, config: Config, datasource_registry: DataSourceRegistry | None = None
    ) -> NamedDataSources:
        """Secondary datasources from ``pyfly.data.relational.datasources.<name>``.

        Inject and call ``.get("<name>")`` for that datasource's ``async_sessionmaker``. A live view
        over the registry: it also lists the datasources a module registers.
        """
        return NamedDataSources.of_registry(_datasources(datasource_registry, config))

    @bean(primary=True)
    @conditional_on_missing_bean(RoutingSessionFactory, singletons_only=True)
    def routing_session_factory(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        config: Config,
        datasource_registry: DataSourceRegistry | None = None,
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
            registry = _datasources(datasource_registry, config)
            replica = registry.primary.replica if registry.has_primary else None
        return RoutingSessionFactory(async_session_factory, replica.sessionmaker if replica is not None else None)

    @bean(primary=True)
    def primary_transaction_manager(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        config: Config,
        datasource_registry: DataSourceRegistry | None = None,
    ) -> SqlAlchemyTransactionManager:
        """The ``primary`` datasource's transaction manager, on the primary ``async_sessionmaker`` bean.

        The ``transaction_manager_registry`` serves the ``primary`` datasource with it
        (:func:`~pyfly.data.relational.sqlalchemy.transaction_manager.bind_primary_session_factory`), so
        ``@transactional``, repository calls outside a transaction, ``SessionProvider``,
        ``infrastructure_unit()`` and the ``AsyncSession`` bean run on one primary: the registry's, or the
        application's singleton ``async_sessionmaker``, ``AsyncEngine`` or ``DataSourceRegistry`` bean that
        replaced it. A primary that still splits from a datasource of the registry logs a WARNING
        (``relational_engine_not_in_registry``, ``relational_primary_on_named_datasource``), and so does a
        session factory bound to no engine, which leaves the units on the registry's primary
        (``relational_session_factory_not_bound``). The binding is undone when the context stops
        (``primary_transaction_manager_binding``): contexts on one ``Config`` share the transaction managers.

        Replace the session factory, the engine or the registry to change the primary, not this bean.
        """
        registry = _datasources(datasource_registry, config)
        manager = bind_primary_session_factory(registry, async_session_factory)
        _warn_if_split_primary(manager, registry)
        return manager

    @bean
    def primary_transaction_manager_binding(
        self,
        primary_transaction_manager: SqlAlchemyTransactionManager,
        config: Config,
        datasource_registry: DataSourceRegistry | None = None,
    ) -> PrimaryTransactionManagerBinding:
        """Unbinds ``primary_transaction_manager`` when the context stops
        (:class:`PrimaryTransactionManagerBinding`)."""
        return PrimaryTransactionManagerBinding(_datasources(datasource_registry, config), primary_transaction_manager)

    @bean(scope=Scope.TRANSIENT)
    def async_session(
        self,
        async_session_factory: async_sessionmaker[AsyncSession],
        primary_transaction_manager: SqlAlchemyTransactionManager,
    ) -> AsyncSession:
        """A ``ScopedAsyncSession`` for the primary datasource — a NEW one for every injection.

        This bean was a singleton until 26.09.06, so every repository, every user bean and the
        engine lifecycle shared one SQLAlchemy session: one transaction, one identity map and one
        connection's local state (``SET LOCAL``, a tenant GUC, a ``search_path``) for the whole
        process. It is transient: each injection is a distinct object.

        Inside a unit of work for its datasource (``@transactional``) the session delegates to the
        unit's session, so a DAO that injects ``AsyncSession`` joins the transaction, and its
        ``commit``/``rollback`` raise (the unit completes itself). Outside a unit it is an ordinary
        session its owner commits and closes. Repositories never receive it: their ``session``
        parameter is ``NoAutowire``, and they resolve their session per call. For custom data access
        code, prefer the ``session_provider`` bean.

        The one session ``engine_lifecycle`` receives is the one it closes at shutdown.
        """
        session: AsyncSession = ScopedAsyncSession.of(
            async_session_factory, datasource=primary_transaction_manager.datasource
        )
        return session

    @bean
    def session_provider(
        self, config: Config, transaction_managers: Provider[TransactionManagerRegistry] | None = None
    ) -> SessionProvider:
        """The session of the current unit of work (``current()``) and programmatic short units
        (``async with provider.unit(read_only=...)``), for custom data access code, on the context's
        ``transaction_manager_registry`` (the managers ``@transactional`` uses)."""
        return SessionProvider(_context_transaction_managers(transaction_managers, config))

    @bean
    def engine_lifecycle(
        self, async_engine: AsyncEngine, async_session: AsyncSession, config: Config
    ) -> EngineLifecycle:
        """Lifecycle bean — applies the schema strategy (``pyfly.data.relational.ddl-auto``) on startup.

        The strategy is resolved against the primary engine's database (``create`` on an embedded database,
        ``none`` on a server or beside startup migrations, when it is not configured); an invalid value
        fails the start here. ``pyfly.data.relational.schema.lock-timeout`` and ``drop-timeout`` bound the
        wait for another instance's schema changes and the ``create-drop`` teardown.

        The engine itself is disposed by the registry, after this lifecycle has stopped.
        """
        datasource = datasource_of(async_engine)
        registry = datasource.registry if datasource is not None else None
        properties = registry.properties if registry is not None else RelationalProperties.from_config(config)
        schema = SchemaInitializer(
            async_engine,
            ddl_auto=config.get("pyfly.data.relational.ddl-auto"),
            migrations=properties.migrations.enabled,
            lock_timeout=properties.schema.lock_timeout,
            drop_timeout=properties.schema.drop_timeout,
        )
        return EngineLifecycle(async_engine, async_session, schema=schema, dispose_engine=registry is None)

    @bean
    def repository_post_processor(
        self, config: Config | None = None, transaction_managers: Provider[TransactionManagerRegistry] | None = None
    ) -> RepositoryBeanPostProcessor:
        """Compiles derived and ``@query`` methods, and binds every repository to the context's
        ``transaction_manager_registry`` (the managers ``@transactional`` uses; without a context,
        repositories use the installed ones)."""
        if config is None and transaction_managers is None:
            return RepositoryBeanPostProcessor()
        return RepositoryBeanPostProcessor(_context_transaction_managers(transaction_managers, config))

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
    @conditional_on_missing_bean(AuditingEntityListener)
    @conditional_on_property("pyfly.data.auditing.enabled", having_value="true", match_if_missing=True)
    def auditing_entity_listener(
        self, auditor_aware: AuditorAware | None = None, date_time_provider: DateTimeProvider | None = None
    ) -> AuditingEntityListener:
        """Stamps ``BaseEntity`` audit columns with the ``AuditorAware`` and ``DateTimeProvider`` beans.

        Registered once per process however many contexts start (the hooks are shared), and unregistered
        when this context stops. An application's own ``AuditingEntityListener`` bean replaces it, and
        ``pyfly.data.auditing.enabled=false`` switches it off.
        """
        listener = AuditingEntityListener(auditor_aware, date_time_provider)
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
@conditional_on_class("sqlalchemy")
@conditional_on_property("pyfly.data.relational.migrations.enabled", having_value="true")
class MigrationAutoConfiguration:
    """Applies Alembic migrations on startup (Spring Boot Flyway-style auto-migrate).

    Opt-in via ``pyfly.data.relational.migrations.enabled=true``; reuses the project's
    Alembic environment (``pyfly db init``). Migrates the same datasource as the app, before the
    schema strategy of ``ddl-auto`` and every other lifecycle bean runs.
    """

    @bean
    def migration_runner(
        self,
        config: Config,
        async_engine: AsyncEngine | None = None,
        datasource_registry: DataSourceRegistry | None = None,
    ) -> MigrationRunner:
        """The runner of the startup migrations, on the application's primary engine (an application's singleton
        ``AsyncEngine`` bean, or the registry's primary), under the schema lock
        (``pyfly.data.relational.schema.lock-timeout``). Without a primary it migrates ``alembic.ini``'s URL."""
        properties = RelationalProperties.from_config(config)
        engine = async_engine
        if engine is None and properties.url:
            engine = _datasources(datasource_registry, config).primary.engine
        return MigrationRunner(
            # The same URL the primary engine uses (the legacy pyfly.data.url alias included).
            url=properties.url or "",
            config_path=properties.migrations.config,
            revision=properties.migrations.revision,
            engine=engine,
            config=config,
            lock_timeout=properties.schema.lock_timeout,
        )
