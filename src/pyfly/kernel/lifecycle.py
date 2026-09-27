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
"""Unified lifecycle protocol for infrastructure adapters, with Spring ``SmartLifecycle``-style phases.

Analogous to Spring's Lifecycle interface. All infrastructure ports that own
connections, pools, or external resources should extend this protocol.
The framework calls start() during context startup and stop() during shutdown.

Every singleton whose class defines both ``start()`` and ``stop()`` is a lifecycle bean, whether a
``@bean`` method or a scanned ``@component``/``@service`` produced it. Each is started once and
stopped once, even when it is registered under several types. The order comes from the bean's
*phase* and, within a phase, from the order in which the beans were created, which puts every bean
after the beans it depends on:

- start runs in ascending phase, and within a phase in creation order;
- stop runs in descending phase, and within a phase in reverse start order.

``ApplicationContext.stop()`` runs in this order:

1. ``ContextClosedEvent`` is published, while every bean still works;
2. the drain: background tasks are cancelled, the ``TaskScheduler`` stops (waiting for the jobs in
   flight), and the lifecycle beans of :data:`CONSUMER_PHASE` or above stop, so that nothing
   dispatches new work into beans that are about to be destroyed;
3. ``@pre_destroy`` runs on every bean, each before the beans it depends on;
4. the other lifecycle beans stop: they own the resources the destroyed beans used (clients,
   schema, the datasource lifecycle);
5. every :class:`ResourceRegistry` bean (the datasource registry) is disposed, last.

Until step 5 a resource registry of the context ignores a request to close itself
(:func:`disposal_deferred`): a lifecycle bean that closes it on stop (the datasource registry's
own) would otherwise close it at that bean's place in the order, before the beans still using it.

After that the context builds no bean until it is started again.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

#: The phase of a lifecycle bean that declares none.
DEFAULT_PHASE = 0

#: The phase of message consumers, pollers and schedulers: they start after every other lifecycle
#: bean and stop first, before ``@pre_destroy``, draining the work in flight. A lifecycle bean that
#: takes subscriptions (it defines ``subscribe``: an event bus, a message broker) is in this phase
#: unless it declares another.
CONSUMER_PHASE = 1 << 30


@runtime_checkable
class Lifecycle(Protocol):
    """Standard lifecycle for infrastructure adapters.

    Adapters that own connections, pools, or external resources implement
    this protocol. The ApplicationContext calls start() during startup
    and stop() during shutdown, ordered by phase and creation (see the module documentation).
    """

    async def start(self) -> None:
        """Initialize connections and validate connectivity.

        Called during application startup. Infrastructure adapters should
        establish their connections here. If the connection fails, raise
        an exception -- the framework wraps it in BeanCreationException
        for fail-fast behavior.
        """
        ...

    async def stop(self) -> None:
        """Release connections and clean up resources.

        Called during application shutdown. Best-effort cleanup -- exceptions
        are logged but do not prevent shutdown of other adapters.
        """
        ...


@runtime_checkable
class SmartLifecycle(Lifecycle, Protocol):
    """A lifecycle bean that declares its phase (Spring's ``SmartLifecycle.getPhase()``).

    A lower phase starts earlier and stops later. Declare it as a class attribute or a property::

        class OutboxRelay:
            phase = CONSUMER_PHASE

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
    """

    @property
    def phase(self) -> int:
        """The phase of this bean (:data:`DEFAULT_PHASE` unless declared)."""
        ...


@runtime_checkable
class ResourceRegistry(Protocol):
    """A bean that owns resources every other bean borrows (connection pools, clients) and releases
    them all at once.

    The context calls :meth:`dispose_all` as the very last step of ``stop()``: after the consumers
    drained, after every ``@pre_destroy`` and after every lifecycle bean stopped. Until then its own
    ``close()`` should check :func:`disposal_deferred` and do nothing.
    :class:`~pyfly.data.relational.datasource_registry.DataSourceRegistry` is one.
    """

    async def dispose_all(self) -> None:
        """Release every resource; idempotent."""
        ...


def lifecycle_phase(bean: Any) -> int:
    """The phase of lifecycle bean *bean*.

    An ``int`` ``phase`` attribute or property wins; otherwise a bean that takes subscriptions (its
    class defines ``subscribe``) is a consumer (:data:`CONSUMER_PHASE`), and any other bean is in
    :data:`DEFAULT_PHASE`.
    """
    try:
        declared = getattr(bean, "phase", None)
    except Exception:  # noqa: BLE001 — a failing property declares nothing
        declared = None
    if isinstance(declared, int) and not isinstance(declared, bool):
        return declared
    if callable(getattr(type(bean), "subscribe", None)):
        return CONSUMER_PHASE
    return DEFAULT_PHASE


def is_resource_registry(bean: Any) -> bool:
    """Whether *bean*'s class defines the coroutine ``dispose_all()`` of a :class:`ResourceRegistry`."""
    return inspect.iscoroutinefunction(getattr(type(bean), "dispose_all", None))


# The resource registries a stopping context disposes last, by identity; empty outside a stop.
_DEFERRED_DISPOSAL: ContextVar[frozenset[int]] = ContextVar("pyfly_deferred_disposal", default=frozenset())


def disposal_deferred(resource: object) -> bool:
    """Whether a stopping context disposes *resource* last, so that its ``close()`` must wait.

    A :class:`ResourceRegistry` calls it at the top of ``close()``: while the context is still
    draining, destroying and stopping the beans that use the registry, a close requested by one of
    them (the registry's lifecycle bean) is left to the final ``dispose_all()``.
    """
    return id(resource) in _DEFERRED_DISPOSAL.get()


@contextmanager
def deferring_disposal(resources: Iterable[object]) -> Iterator[None]:
    """Within the block, :func:`disposal_deferred` answers ``True`` for *resources* (and the tasks the
    block starts inherit it)."""
    token = _DEFERRED_DISPOSAL.set(_DEFERRED_DISPOSAL.get() | {id(resource) for resource in resources})
    try:
        yield
    finally:
        _DEFERRED_DISPOSAL.reset(token)
