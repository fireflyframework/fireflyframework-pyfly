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
"""Application events and event bus for context lifecycle notifications."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pyfly.container.ordering import get_order

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


def app_event_listener(func: F) -> F:
    """Mark a method as a listener for application events.

    The event type is inferred from the method's type hint on the event parameter.
    """
    func.__pyfly_app_event_listener__ = True  # type: ignore[attr-defined]
    return func


class ApplicationEventBus:
    """Simple in-process event bus for application lifecycle events."""

    def __init__(self) -> None:
        self._listeners: dict[
            type,
            list[tuple[Callable[..., Awaitable[None]], type | None]],
        ] = {}

    def subscribe(
        self,
        event_type: type,
        listener: Callable[..., Awaitable[None]],
        *,
        owner_cls: type | None = None,
    ) -> None:
        """Register a listener for a specific event type (any type, not only ApplicationEvent)."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append((listener, owner_cls))
        # Pre-sort so publish() doesn't need to sort per invocation
        self._listeners[event_type].sort(key=lambda e: get_order(e[1]) if e[1] else 0)

    async def publish(self, event: object) -> None:
        """Publish an event to all matching listeners (pre-sorted by @order).

        *event* may be any object — lifecycle ``ApplicationEvent`` subclasses or arbitrary
        domain events. Listeners may be synchronous (``void``) or coroutine functions; the
        result is awaited only when awaitable, so a plain ``def`` listener does not crash
        startup (audit #115).
        """
        for event_type, entries in self._listeners.items():
            if isinstance(event, event_type):
                for listener, _owner in entries:
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
