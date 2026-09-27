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
"""The transaction managers of an application, by datasource name.

A :class:`TransactionManagerRegistry` maps datasource names to
:class:`~pyfly.data.transaction.manager.TransactionManager` instances. Managers are registered directly,
or produced on demand by *resolvers* a backend adds (the relational auto-configuration adds one that
builds a manager for every datasource of the ``DataSourceRegistry``, including datasources registered
later). It also finds the manager of a legacy resource by identity (an ``async_sessionmaker``, an
``AsyncMongoClient``).

The application context installs its registry when it starts and removes it when it stops (only while it
is still the installed one), so code that names no manager (``@transactional`` on a plain function, a
service with no factory attribute) runs on the installed registry's default datasource.

:func:`resolve_manager` turns what a caller has (a datasource name, a manager, a legacy resource object)
into a manager. For a resource it asks the backend adapters, which are imported lazily so this package
stays free of SQLAlchemy and pymongo.
"""

from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionManager

_logger = logging.getLogger(__name__)

PRIMARY = "primary"
"""The default datasource name."""

ManagerResolver = Callable[[str], "TransactionManager | None"]
"""Returns the manager of a datasource name, or ``None`` when the resolver does not know it."""

ResourceResolver = Callable[[object], "TransactionManager | None"]
"""Returns the manager that owns a resource object (a session factory, a client), or ``None``."""

# Backend adapters that register a resource resolver when imported (see register_resource_resolver).
_ADAPTER_MODULES: list[str] = ["pyfly.data.relational.sqlalchemy.transaction_manager"]
_RESOURCE_RESOLVERS: list[ResourceResolver] = []
_adapters_loaded = False
_adapters_lock = threading.Lock()


def register_resource_resolver(resolver: ResourceResolver) -> None:
    """Let *resolver* map resource objects to their managers in :func:`resolve_manager` (backend adapters
    call it when they are imported)."""
    if resolver not in _RESOURCE_RESOLVERS:
        _RESOURCE_RESOLVERS.append(resolver)


def register_adapter_module(module: str) -> None:
    """Name a backend adapter module that :func:`resolve_manager` imports before resolving a resource."""
    global _adapters_loaded
    with _adapters_lock:
        if module not in _ADAPTER_MODULES:
            _ADAPTER_MODULES.append(module)
            _adapters_loaded = False


def _load_adapters() -> None:
    global _adapters_loaded
    if _adapters_loaded:
        return
    with _adapters_lock:
        for module in list(_ADAPTER_MODULES):
            try:
                importlib.import_module(module)
            except ImportError:
                _logger.debug("transaction_adapter_unavailable", extra={"module": module})
        _adapters_loaded = True


class TransactionManagerRegistry:
    """The transaction managers of one application, by datasource name.

    ``default`` names the datasource a boundary that names none runs on (``"primary"``). ``metrics``, when
    set, receives the ``pyfly.tx.synchronization.failures`` counter.
    """

    def __init__(self, *, default: str = PRIMARY, metrics: Any = None) -> None:
        self._default = default
        self._managers: dict[str, TransactionManager] = {}
        self._resolvers: list[ManagerResolver] = []
        self._resource_resolvers: list[ResourceResolver] = []
        self._lock = threading.RLock()
        self.metrics = metrics

    @property
    def default_name(self) -> str:
        """The name of the default datasource."""
        return self._default

    def register(self, manager: TransactionManager, *, default: bool = False) -> None:
        """Register *manager* under its datasource name (and make it the default when *default*)."""
        with self._lock:
            self._managers[manager.datasource] = manager
            if default:
                self._default = manager.datasource

    def unregister(self, manager: TransactionManager) -> bool:
        """Remove *manager* if it is the one registered under its datasource name (another registered since
        stays); returns whether it was. A resolver then serves that name again."""
        with self._lock:
            name = manager.datasource
            if self._managers.get(name) is not manager:
                return False
            del self._managers[name]
            return True

    def add_resolver(self, resolver: ManagerResolver) -> None:
        """Ask *resolver* for a datasource name no registered manager serves."""
        with self._lock:
            self._resolvers.append(resolver)

    def add_resource_resolver(self, resolver: ResourceResolver) -> None:
        """Ask *resolver* for the manager of a legacy resource object (see :meth:`find_by_resource`)."""
        with self._lock:
            self._resource_resolvers.append(resolver)

    def find(self, name: str) -> TransactionManager | None:
        """The manager of datasource *name*, or ``None``."""
        with self._lock:
            manager = self._managers.get(name)
            resolvers = list(self._resolvers)
        if manager is not None:
            return manager
        for resolver in resolvers:
            resolved = resolver(name)
            if resolved is not None:
                with self._lock:
                    self._managers.setdefault(name, resolved)
                return resolved
        return None

    def get(self, name: str | None = None) -> TransactionManager:
        """The manager of datasource *name* (the default datasource when ``None``).

        Raises :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` when there is none.
        """
        target = self._default if name is None else name
        manager = self.find(target)
        if manager is None:
            raise IllegalTransactionStateError(
                f"No transaction manager for datasource {target!r}; known: {self.names()}. Configure the "
                "datasource (pyfly.data.relational.datasources.<name>.url) or name one that exists.",
                datasource=target,
            )
        return manager

    @property
    def default(self) -> TransactionManager | None:
        """The manager of the default datasource, or ``None`` when it is not configured."""
        return self.find(self._default)

    def find_by_resource(self, resource: object) -> TransactionManager | None:
        """The manager that owns *resource* (a session factory, a Mongo client), by identity."""
        with self._lock:
            managers = list(self._managers.values())
            resolvers = list(self._resource_resolvers)
        for manager in managers:
            owns = getattr(manager, "owns", None)
            if callable(owns) and owns(resource):
                return manager
        for resolver in resolvers:
            resolved = resolver(resource)
            if resolved is not None:
                return resolved
        return None

    def names(self) -> list[str]:
        """The names of the managers registered or resolved so far."""
        with self._lock:
            return sorted(self._managers)

    def __repr__(self) -> str:
        return f"TransactionManagerRegistry(default={self._default!r}, managers={self.names()})"


# -- the installed registry ----------------------------------------------------------------------------

_installed: TransactionManagerRegistry | None = None
_installed_lock = threading.Lock()


def install_registry(registry: TransactionManagerRegistry) -> None:
    """Make *registry* the one boundaries without an explicit manager use (the context's start does it)."""
    global _installed
    with _installed_lock:
        _installed = registry


def uninstall_registry(registry: TransactionManagerRegistry) -> None:
    """Remove *registry*, only if it is still the installed one (a later context may have replaced it)."""
    global _installed
    with _installed_lock:
        if _installed is registry:
            _installed = None


def installed_registry() -> TransactionManagerRegistry | None:
    """The installed registry, or ``None`` outside a started application context."""
    return _installed


def resolve_manager(target: object = None) -> TransactionManager:
    """The transaction manager for *target*.

    - ``None``: the installed registry's default datasource;
    - a datasource name: that datasource's manager in the installed registry;
    - a :class:`~pyfly.data.transaction.manager.TransactionManager`: itself;
    - anything else (a ``DataSource``, an ``async_sessionmaker``, an ``AsyncMongoClient``...): the manager
      a backend adapter or the installed registry maps it to.

    Raises :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` when nothing resolves.
    """
    registry = installed_registry()
    if target is None or isinstance(target, str):
        if registry is None:
            name = PRIMARY if target is None else target
            raise IllegalTransactionStateError(
                f"No transaction manager for datasource {name!r}: no application context is running. Start an "
                "ApplicationContext with the data auto-configuration, run inside @transactional, or pass the "
                "manager (or the session factory) explicitly.",
                datasource=name,
            )
        return registry.get(target)
    if isinstance(target, TransactionManager):
        return target
    manager = find_manager_for_resource(target)
    if manager is None:
        raise IllegalTransactionStateError(
            f"No transaction manager owns {type(target).__name__} {target!r}; pass a datasource name, a "
            "TransactionManager or a session factory the application built."
        )
    return manager


def find_manager_for_resource(resource: object) -> TransactionManager | None:
    """The manager of *resource* from the installed registry or a backend adapter, or ``None``."""
    registry = installed_registry()
    if registry is not None:
        manager = registry.find_by_resource(resource)
        if manager is not None:
            return manager
    _load_adapters()
    for resolver in list(_RESOURCE_RESOLVERS):
        manager = resolver(resource)
        if manager is not None:
            return manager
    return None
