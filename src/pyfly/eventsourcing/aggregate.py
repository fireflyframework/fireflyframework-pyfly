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
"""AggregateRoot — base class for event-sourced entities.

Subclasses register apply-handlers via :meth:`when` and emit events via
:meth:`apply`.  Pending events are flushed to the event store by the
repository after each command handler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyfly.eventsourcing.event import DomainEvent


@dataclass
class AggregateRoot:
    """Base aggregate root with built-in event sourcing machinery."""

    id: str = ""
    version: int = 0
    _pending_events: list[DomainEvent] = field(default_factory=list, init=False, repr=False)
    _handlers: dict[str, Callable[[Any, Any], None]] = field(default_factory=dict, init=False, repr=False)

    def when(
        self,
        event_type: type | str,
        handler: Callable[[Any, Any], None],
    ) -> None:
        """Register an apply-handler for *event_type* (class **or** string name)."""
        key = event_type if isinstance(event_type, str) else event_type.__name__
        self._handlers[key] = handler

    def apply(self, event: DomainEvent) -> None:
        """Apply a new event: route to handler, increment version, queue for persist."""
        self._dispatch(event.event_type, event)
        self._pending_events.append(event)

    def replay(self, event_type: str, event: Any) -> None:
        """Re-apply a persisted event by event_type string."""
        self._dispatch(event_type, event)

    def pending_events(self) -> list[DomainEvent]:
        return list(self._pending_events)

    def mark_committed(self) -> None:
        self._pending_events.clear()

    # -- internals ----------------------------------------------------------

    def _dispatch(self, event_type: str, event: Any) -> None:
        handler = self._handlers.get(event_type)
        if handler is not None:
            handler(self, event)
        elif hasattr(self, f"on_{event_type}"):
            getattr(self, f"on_{event_type}")(event)
        self.version += 1
