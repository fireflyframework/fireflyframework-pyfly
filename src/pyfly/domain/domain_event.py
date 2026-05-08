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
"""DDD :class:`DomainEvent` — something that happened in the domain.

This is the *non-event-sourced* counterpart to
:class:`pyfly.eventsourcing.DomainEvent`. The two are deliberately
different: the event-sourcing variant carries an ``event_type`` string
and is replayed to rebuild aggregate state, while this variant is a
plain immutable record collected by an aggregate during a transaction
and dispatched by the application service after the unit of work
commits.

Subclasses must be ``@dataclass(frozen=True)``. The base class assigns a
UUID and a UTC timestamp at construction time so every event is
self-identifying.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class DomainEvent:
    """Base for transient domain events raised by aggregates."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def event_type(self) -> str:
        """Logical event type — defaults to the subclass name."""
        return type(self).__name__
