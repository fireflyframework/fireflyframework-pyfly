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
"""Document data layer (MongoDB/Beanie) auto-configuration.

With Beanie installed, :class:`DocumentAutoConfiguration` always registers the :class:`MongoRepositoryWiringCheck`,
which fails the start on a MongoDB repository whose derived or ``@query`` stubs no post-processor compiled (the
document layer is not enabled: they would answer ``None``). With ``pyfly.data.document.enabled=true`` it adds:

- ``mongo_client``: the ``AsyncMongoClient`` built from :class:`~pyfly.config.properties.mongodb.DocumentProperties`
  (pool, timeouts, ``tz_aware=True``, ``uuidRepresentation="standard"``, the ``options`` map), with the
  command and pool metrics listener; contexts that configure the same client share it
  (:mod:`~pyfly.data.document.mongodb.initializer`);
- ``odm_initializer``: the :class:`~pyfly.data.document.mongodb.initializer.BeanieInitializer`;
- ``mongo_post_processor``: compiles the repositories' query methods and binds them to the context's
  transaction managers;
- ``mongo_transaction_manager``: the :class:`~pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager`
  of the client, registered in the context's ``TransactionManagerRegistry`` under ``pyfly.data.document.datasource``
  (``"document"``) while the context runs (``mongo_transaction_manager_registration``), so ``@transactional``
  finds it by ``datasource=``, by a service's ``_motor_client``, or as the default datasource of an application
  without a relational one (``pyfly.data.document.transaction.default``);
- ``mongo_health_indicator``: the readiness check of the datasource;
- ``document_auditing_handler``: stamps ``BaseDocument`` writes (``pyfly.data.auditing.enabled``).
"""

# NOTE: No `from __future__ import annotations` — typing.get_type_hints()
# must resolve return types at runtime for @bean method registration.

import logging
from collections.abc import Callable
from typing import Any

try:
    from pymongo import AsyncMongoClient
except ImportError:
    AsyncMongoClient = object  # type: ignore[misc,assignment]

from pyfly.config.properties.mongodb import DocumentProperties, document_is_default_datasource
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError, NoUniqueBeanError
from pyfly.container.ordering import HIGHEST_PRECEDENCE, order
from pyfly.container.provider import Provider
from pyfly.context.conditions import (
    auto_configuration,
    conditional_on_class,
    conditional_on_missing_bean,
    conditional_on_property,
)
from pyfly.core.config import Config
from pyfly.data.auditing import AuditorAware, DateTimeProvider
from pyfly.data.document.mongodb.document import DocumentAuditingHandler
from pyfly.data.document.mongodb.health import MongoHealthIndicator, MongoMetrics
from pyfly.data.document.mongodb.initializer import BINDINGS, BeanieInitializer
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction.registry import TransactionManagerRegistry, install_registry, uninstall_registry

try:
    from pyfly.observability.metrics import MetricsRegistry
except ImportError:
    MetricsRegistry = object  # type: ignore[misc,assignment]

_logger = logging.getLogger(__name__)

_ENABLED = "pyfly.data.document.enabled"


def _lookup(provider: Provider[Any] | None) -> Callable[[], Any]:
    """A callable that resolves *provider*'s bean when first asked, or answers ``None`` when there is none."""

    def resolve() -> Any:
        if provider is None:
            return None
        try:
            return provider.get()
        except (NoSuchBeanError, NoUniqueBeanError):
            return None

    return resolve


class MongoTransactionManagerRegistration:
    """Registers the document datasource's transaction manager while the context runs.

    ``start()`` registers it in the context's ``TransactionManagerRegistry`` (the relational
    auto-configuration's, whenever SQLAlchemy is installed), making it the default datasource when *default*
    says so, and makes it the manager of its client (``MongoTransactionManager.for_client``). Without such a
    registry (no SQLAlchemy), it installs one of its own with the document datasource as the default.
    ``stop()`` undoes all of it, restoring the previous default. It starts before the other lifecycle beans
    and stops after them.
    """

    #: The lowest phase: registered before any lifecycle bean runs a unit of work, removed after the last.
    phase = -(1 << 30)

    def __init__(
        self,
        manager: MongoTransactionManager,
        registry: Callable[[], TransactionManagerRegistry | None],
        *,
        default: bool,
        metrics: Callable[[], Any] | None = None,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._default = default
        self._metrics = metrics
        self._registered: TransactionManagerRegistry | None = None
        self._owned = False
        self._previous_default: str | None = None

    async def start(self) -> None:
        """Register the manager (see the class documentation); idempotent."""
        if self._registered is not None:
            return
        registry = self._registry()
        owned = registry is None
        if registry is None:
            registry = TransactionManagerRegistry(default=self._manager.datasource)
            recorder = self._metrics() if self._metrics is not None else None
            if recorder is not None:
                registry.metrics = recorder
        registry.register(self._manager)
        if self._default and not owned:
            self._previous_default = registry.set_default(self._manager.datasource)
        self._manager.attach()
        if owned:
            install_registry(registry)
        self._registered = registry
        self._owned = owned

    async def stop(self) -> None:
        """Unregister the manager, restore the previous default, and uninstall an own registry (idempotent)."""
        registry, self._registered = self._registered, None
        if registry is None:
            return
        registry.unregister(self._manager)
        if self._previous_default is not None and registry.default_name == self._manager.datasource:
            registry.set_default(self._previous_default)
        self._previous_default = None
        self._manager.detach()
        if self._owned:
            uninstall_registry(registry)


@order(HIGHEST_PRECEDENCE + 200)
class MongoRepositoryWiringCheck:
    """``BeanPostProcessor`` that fails the start on a MongoDB repository whose query stubs were never compiled.

    The document post-processor (``pyfly.data.document.enabled=true``) replaces a repository's derived
    (``find_by_*``, ``count_by_*``, ``exists_by_*``, ``delete_by_*``) and ``@query`` stubs with compiled queries.
    Without it the stubs keep their ``...`` bodies and answer ``None``. It runs right after the repository
    post-processors, before the AOP one.
    """

    def before_init(self, bean: Any, bean_name: str) -> Any:
        """Pass through."""
        return bean

    def after_init(self, bean: Any, bean_name: str) -> Any:
        """Refuse *bean* when it is a MongoDB repository with a derived or ``@query`` stub no post-processor
        compiled."""
        if isinstance(bean, MongoRepository):
            stubs = _uncompiled_query_methods(bean)
            if stubs:
                raise BeanCreationException(
                    subsystem="data",
                    provider=type(bean).__name__,
                    reason=(
                        f"{type(bean).__name__} ({bean_name}) is a MongoDB repository whose query methods "
                        f"({', '.join(stubs)}) no repository post-processor compiled: they would answer None "
                        "without touching the database. The document data layer is not enabled: set "
                        "pyfly.data.document.enabled=true (with pyfly.data.document.uri)."
                    ),
                )
        return bean


def _uncompiled_query_methods(bean: Any) -> list[str]:
    """The derived and ``@query`` stubs of *bean*'s class that no post-processor replaced on *bean*."""
    from pyfly.data.post_processor import declared_methods, is_derived_query_name, is_stub

    inherited = set(dir(MongoRepository))
    compiled = vars(bean)
    return [
        name
        for name, method in declared_methods(type(bean), MongoRepository)
        if name not in compiled
        and (
            hasattr(method, "__pyfly_query__")
            or (name not in inherited and is_derived_query_name(name) and is_stub(method))
        )
    ]


@auto_configuration
@conditional_on_class("beanie")
class DocumentAutoConfiguration:
    """Auto-configures the MongoDB client, Beanie, the repositories, transactions, health and auditing (see the
    module documentation)."""

    @bean
    @conditional_on_missing_bean(MongoRepositoryWiringCheck)
    def mongo_repository_wiring_check(self) -> MongoRepositoryWiringCheck:
        """Fails the start on a MongoDB repository whose query stubs were never compiled."""
        return MongoRepositoryWiringCheck()

    @bean(primary=True)
    @conditional_on_property(_ENABLED, having_value="true")
    @conditional_on_missing_bean(AsyncMongoClient, singletons_only=True)
    def mongo_client(self, config: Config, metrics: Provider[MetricsRegistry] | None = None) -> AsyncMongoClient:  # type: ignore[type-arg]
        """The application's Mongo client, from ``pyfly.data.document.*`` (module documentation).

        A singleton ``AsyncMongoClient`` bean of the application replaces it. A request- or refresh-scoped one is
        a second client, and this one stays the ``@primary`` candidate.
        """
        properties = DocumentProperties.from_config(config)
        options = properties.client_options()
        listener = MongoMetrics(_lookup(metrics), datasource=properties.datasource)
        options["event_listeners"] = [*options.get("event_listeners", ()), listener]
        return BINDINGS.client_for(properties.uri, options)

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    def mongo_post_processor(
        self, transaction_managers: Provider[TransactionManagerRegistry] | None = None
    ) -> MongoRepositoryBeanPostProcessor:
        """Compiles derived and ``@query`` methods, and binds every repository to the context's transaction
        managers."""
        return MongoRepositoryBeanPostProcessor(_lookup(transaction_managers))

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    def odm_initializer(
        self,
        config: Config,
        container: Container,
        mongo_client: AsyncMongoClient,  # type: ignore[type-arg]
    ) -> BeanieInitializer:
        """Binds the document classes to the database when the context starts."""
        return BeanieInitializer(motor_client=mongo_client, config=config, container=container)

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    @conditional_on_missing_bean(MongoTransactionManager)
    def mongo_transaction_manager(
        self,
        config: Config,
        mongo_client: AsyncMongoClient,  # type: ignore[type-arg]
    ) -> MongoTransactionManager:
        """The document datasource's transaction manager (``pyfly.data.document.transaction.*``)."""
        properties = DocumentProperties.from_config(config)
        return MongoTransactionManager(
            mongo_client,
            datasource=properties.datasource,
            read_concern=properties.transaction.read_concern,
            write_concern=properties.transaction.write_concern,
            max_commit_time=properties.transaction.max_commit_time,
        )

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    def mongo_transaction_manager_registration(
        self,
        config: Config,
        mongo_transaction_manager: MongoTransactionManager,
        transaction_managers: Provider[TransactionManagerRegistry] | None = None,
        metrics: Provider[MetricsRegistry] | None = None,
    ) -> MongoTransactionManagerRegistration:
        """Registers the manager in the context's transaction managers while the context runs, as their default
        when :func:`~pyfly.config.properties.mongodb.document_is_default_datasource` says so."""
        return MongoTransactionManagerRegistration(
            mongo_transaction_manager,
            _lookup(transaction_managers),
            default=document_is_default_datasource(config),
            metrics=_lookup(metrics),
        )

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    def mongo_health_indicator(
        self,
        config: Config,
        mongo_client: AsyncMongoClient,  # type: ignore[type-arg]
        mongo_transaction_manager: MongoTransactionManager,
    ) -> MongoHealthIndicator:
        """The readiness check of the document datasource (``pyfly.data.document.health.timeout``)."""
        properties = DocumentProperties.from_config(config)
        return MongoHealthIndicator(
            mongo_client,
            datasource=properties.datasource,
            database=properties.database,
            timeout=properties.health.timeout,
            manager=mongo_transaction_manager,
        )

    @bean
    @conditional_on_property(_ENABLED, having_value="true")
    @conditional_on_property("pyfly.data.auditing.enabled", having_value="true", match_if_missing=True)
    @conditional_on_missing_bean(DocumentAuditingHandler)
    def document_auditing_handler(
        self,
        auditor_aware: Provider[AuditorAware] | None = None,
        date_time_provider: Provider[DateTimeProvider] | None = None,
    ) -> DocumentAuditingHandler:
        """Stamps ``BaseDocument`` writes with the application's ``AuditorAware`` and ``DateTimeProvider``
        (resolved when the context starts)."""
        return _LazyDocumentAuditingHandler(_lookup(auditor_aware), _lookup(date_time_provider))


class _LazyDocumentAuditingHandler(DocumentAuditingHandler):
    """A :class:`DocumentAuditingHandler` whose ports are the context's beans, looked up when it starts (they may
    be registered after the document beans are built)."""

    def __init__(self, auditor_aware: Callable[[], Any], date_time_provider: Callable[[], Any]) -> None:
        super().__init__()
        self._resolve_auditor = auditor_aware
        self._resolve_clock = date_time_provider

    async def start(self) -> None:
        auditor = self._resolve_auditor()
        clock = self._resolve_clock()
        if auditor is not None and callable(getattr(auditor, "get_current_auditor", None)):
            self._auditor_aware = auditor
        if clock is not None and callable(getattr(clock, "get_now", None)):
            self._date_time_provider = clock
        await super().start()
