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
The application service collects ``pending_events()`` after each unit of
work and publishes them through the event bus.

This is the *non-event-sourced* aggregate. It does **not** rebuild from
its event log — state is persisted directly through repositories. For
the event-sourced variant (with ``apply``/``replay``/``when``) see
:class:`pyfly.eventsourcing.AggregateRoot`.
"""

from __future__ import annotations

from typing import TypeVar

from pyfly.domain.domain_event import DomainEvent
from pyfly.domain.entity import Entity

TID = TypeVar("TID")


class AggregateRoot(Entity[TID]):
    """Base class for non-event-sourced aggregate roots."""

    __slots__ = ("_pending_events",)

    def __init__(self, id: TID | None = None) -> None:
        super().__init__(id)
        self._pending_events: list[DomainEvent] = []

    def raise_event(self, event: DomainEvent) -> None:
        """Queue *event* for publication after the unit of work commits."""
        self._pending_events.append(event)

    def pending_events(self) -> list[DomainEvent]:
        """Return a snapshot of the pending events.

        The list is copied so callers can iterate safely while the
        aggregate continues to raise more events.
        """
        return list(self._pending_events)

    def clear_events(self) -> list[DomainEvent]:
        """Drain the pending events and return them.

        Repositories or unit-of-work coordinators should call this once
        the aggregate has been persisted, then publish the returned
        events to the event bus.
        """
        events = self._pending_events
        self._pending_events = []
        return events
