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
"""Application events and event bus for context lifecycle notifications.

A listener runs inline in :meth:`ApplicationEventBus.publish`, inside the caller's transaction when there is
one, unless it declares the transaction phase it runs at (Spring's ``@TransactionalEventListener``)::

    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def send_receipt(self, event: OrderPlaced) -> None: ...

- ``BEFORE_COMMIT``: inside the unit of work, right before it commits;
- ``AFTER_COMMIT``: once it committed, outside it (not at all when it rolls back);
- ``AFTER_ROLLBACK``: only once it rolled back;
- ``AFTER_COMPLETION``: once it completed, either way.

Outside a transaction there is nothing to wait for: every phase but ``AFTER_ROLLBACK`` runs at once, and an
``AFTER_ROLLBACK`` listener does not run. A failure of an ``AFTER_*`` listener is logged and counted, never
raised: the unit has completed (see :mod:`pyfly.data.transaction.synchronization`).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar, overload

from pyfly.container.ordering import get_order

if TYPE_CHECKING:
    from pyfly.data.transaction.synchronization import TransactionPhase

F = TypeVar("F", bound=Callable[..., Any])


class ApplicationEvent:
    """Base class for all application lifecycle events."""


class ContextRefreshedEvent(ApplicationEvent):
    """Published when the ApplicationContext is fully initialized."""


class ApplicationReadyEvent(ApplicationEvent):
    """Published when the application is ready to serve requests."""


class ContextClosedEvent(ApplicationEvent):
    """Published when the ApplicationContext is shutting down."""


class RefreshScopeRefreshedEvent(ApplicationEvent):
    """Published after a refresh evicts refresh-scoped beans (Spring Cloud parity).

    ``refreshed`` holds the cache keys of the evicted beans.
    """

    def __init__(self, refreshed: list[str]) -> None:
        self.refreshed = refreshed


@overload
def app_event_listener(func: F, /) -> F: ...
@overload
def app_event_listener(*, phase: TransactionPhase | str | None = None) -> Callable[[F], F]: ...
def app_event_listener(func: Any = None, /, *, phase: Any = None) -> Any:
    """Mark a method as a listener for application events, bare or with the *phase* it runs at.

    The event type is inferred from the method's type hint on the event parameter. *phase* is a
    :class:`~pyfly.data.transaction.TransactionPhase` (or its name): the listener then runs at that phase of
    the unit of work the event is published in (see the module documentation); without one it runs inline.
    """

    def mark(target: F) -> F:
        target.__pyfly_app_event_listener__ = True  # type: ignore[attr-defined]
        target.__pyfly_event_phase__ = _phase(phase)  # type: ignore[attr-defined]
        return target

    if func is not None:
        return mark(func)
    return mark


def _phase(phase: Any) -> TransactionPhase | None:
    if phase is None:
        return None
    from pyfly.data.transaction.synchronization import TransactionPhase

    return phase if isinstance(phase, TransactionPhase) else TransactionPhase(str(phase).upper())


class ApplicationEventBus:
    """Simple in-process event bus for application lifecycle events."""

    def __init__(self) -> None:
        # Per event type: (listener, owner class for @order, owning bean or None, transaction phase or None).
        self._listeners: dict[
            type,
            list[tuple[Callable[..., Awaitable[None]], type | None, object | None, TransactionPhase | None]],
        ] = {}

    def subscribe(
        self,
        event_type: type,
        listener: Callable[..., Awaitable[None]],
        *,
        owner_cls: type | None = None,
        owner: object | None = None,
        phase: TransactionPhase | str | None = None,
    ) -> None:
        """Register a listener for a specific event type (any type, not only ApplicationEvent).

        *owner* is the bean the listener belongs to; :meth:`unsubscribe_owners` removes its listeners
        when the bean is destroyed. *phase* is the transaction phase the listener runs at (by default the
        one its ``@app_event_listener(phase=...)`` declares; none: inline).
        """
        if phase is None:
            phase = getattr(listener, "__pyfly_event_phase__", None)
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append((listener, owner_cls, owner, _phase(phase)))
        # Pre-sort so publish() doesn't need to sort per invocation
        self._listeners[event_type].sort(key=lambda e: get_order(e[1]) if e[1] else 0)

    def unsubscribe_owners(self, owners: Iterable[object]) -> int:
        """Remove every listener subscribed on behalf of one of *owners* (by identity); returns how many.

        The context calls it on stop() for the beans it destroyed, so a restarted context does not
        deliver events to the previous run's instances as well as to the new ones.
        """
        owned = {id(owner) for owner in owners}
        removed = 0
        for event_type, entries in list(self._listeners.items()):
            kept = [entry for entry in entries if entry[2] is None or id(entry[2]) not in owned]
            removed += len(entries) - len(kept)
            if kept:
                self._listeners[event_type] = kept
            else:
                del self._listeners[event_type]
        return removed

    def listener_count(self, event_type: type | None = None) -> int:
        """How many listeners are subscribed to *event_type* (to every type when ``None``)."""
        if event_type is not None:
            return len(self._listeners.get(event_type, ()))
        return sum(len(entries) for entries in self._listeners.values())

    async def publish(self, event: object) -> None:
        """Publish an event to all matching listeners (pre-sorted by @order).

        *event* may be any object — lifecycle ``ApplicationEvent`` subclasses or arbitrary
        domain events. Listeners may be synchronous (``void``) or coroutine functions; the
        result is awaited only when awaitable, so a plain ``def`` listener does not crash
        startup (audit #115). A listener with a transaction phase is registered on the current unit of
        work, to run at that phase (see the module documentation).
        """
        for event_type, entries in list(self._listeners.items()):
            if isinstance(event, event_type):
                for listener, _owner_cls, _owner, phase in list(entries):
                    if phase is not None:
                        from pyfly.data.transaction.synchronization import on_phase

                        await on_phase(phase, functools.partial(listener, event))
                        continue
                    result = listener(event)
                    if inspect.isawaitable(result):
                        await result


class ApplicationEventPublisher:
    """Injectable publisher for firing application events into the context event bus.

    Inject it into any bean and publish lifecycle or arbitrary domain events::

        @service
        class OrderService:
            def __init__(self, events: ApplicationEventPublisher) -> None:
                self._events = events

            async def place(self, order: Order) -> None:
                await self._events.publish(OrderPlacedEvent(order.id))

    Any ``@app_event_listener`` whose parameter type matches the published event (by
    ``isinstance``) is invoked. The Spring ``ApplicationEventPublisher`` equivalent.
    """

    def __init__(self, bus: ApplicationEventBus) -> None:
        self._bus = bus

    async def publish(self, event: object) -> None:
        """Publish *event* (any object) to all matching listeners."""
        await self._bus.publish(event)
