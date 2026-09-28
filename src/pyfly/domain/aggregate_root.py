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
"""DDD :class:`AggregateRoot` — entity that owns a consistency boundary.

An aggregate root is the only object inside an aggregate that the rest
of the system holds a reference to. State changes happen through method
calls on the root, which optionally raise :class:`DomainEvent` instances.

The events are published when the unit of work that persists the aggregate
commits, never before: the application's ``DomainEventPublisher``
(:mod:`pyfly.eda.domain_events`) collects the events an aggregate raises inside
a unit of work (and those of an aggregate a unit of work saves) and publishes
them as the unit commits, to the application's event listeners and, when
configured, through the transactional outbox. A unit that rolls back publishes
nothing. Code that publishes by hand drains the events with :meth:`clear_events`
inside the unit of work.

This is the *non-event-sourced* aggregate. It does **not** rebuild from
its event log — state is persisted directly through repositories. For
the event-sourced variant (with ``apply``/``replay``/``when``) see
:class:`pyfly.eventsourcing.AggregateRoot`.

It may be mixed into an ORM-mapped entity (``class Order(Base, AggregateRoot[int])``): an instance the ORM
loads without running :meth:`__init__` still collects events.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.domain.domain_event import DomainEvent
from pyfly.domain.entity import Entity

TID = TypeVar("TID")

EventObserver = Callable[["AggregateRoot[Any]", DomainEvent], None]
"""Told of every event an aggregate raises, as it is raised (see :func:`add_event_observer`)."""

_LOCK = threading.Lock()
_OBSERVERS: tuple[EventObserver, ...] = ()


def add_event_observer(observer: EventObserver) -> None:
    """Call *observer* with the aggregate and the event each time an aggregate raises one (idempotent).

    Infrastructure uses it to tie the event to the running unit of work; an observer must not raise.
    """
    global _OBSERVERS
    with _LOCK:
        if observer not in _OBSERVERS:
            _OBSERVERS = (*_OBSERVERS, observer)


def remove_event_observer(observer: EventObserver) -> None:
    """Stop calling *observer* (idempotent)."""
    global _OBSERVERS
    with _LOCK:
        _OBSERVERS = tuple(registered for registered in _OBSERVERS if registered != observer)


class AggregateRoot(Entity[TID]):
    """Base class for non-event-sourced aggregate roots."""

    __slots__ = ("_pending_events",)

    def __init__(self, id: TID | None = None) -> None:
        super().__init__(id)
        self._pending_events: list[DomainEvent] = []

    def _events(self) -> list[DomainEvent]:
        try:
            return self._pending_events
        except AttributeError:
            # Built without __init__ (an ORM loading a mapped aggregate): start the buffer now.
            self._pending_events = []
            return self._pending_events

    def raise_event(self, event: DomainEvent) -> None:
        """Queue *event* for publication when the unit of work commits."""
        self._events().append(event)
        for observer in _OBSERVERS:
            observer(self, event)

    def pending_events(self) -> list[DomainEvent]:
        """Return a snapshot of the pending events.

        The list is copied so callers can iterate safely while the
        aggregate continues to raise more events.
        """
        return list(self._events())

    def clear_events(self) -> list[DomainEvent]:
        """Drain the pending events and return them.

        The domain-event publisher calls this as the unit of work that
        persists the aggregate commits; code that publishes the events
        itself calls it inside that unit of work.
        """
        events = self._events()
        self._pending_events = []
        return events
