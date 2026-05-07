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
"""PyFly event-sourcing — aggregates, event store, snapshots, transactional outbox.

Mirrors ``org.fireflyframework.eventsourcing``:

* :class:`AggregateRoot` — base class for event-sourced entities
* :class:`DomainEvent` — typed event marker
* :class:`EventStore` — append/load/snapshot SPI
* :class:`InMemoryEventStore` — default zero-dep adapter
* :class:`SqlAlchemyEventStore` — async SQL adapter
* :class:`SnapshotStore` — snapshot SPI + in-memory adapter
* :class:`TransactionalOutbox` — event publisher with at-least-once guarantees
* :class:`Projection` — read-model projector
"""

from __future__ import annotations

from pyfly.eventsourcing.aggregate import AggregateRoot
from pyfly.eventsourcing.event import DomainEvent, StoredEventEnvelope, domain_event
from pyfly.eventsourcing.outbox import OutboxRecord, TransactionalOutbox
from pyfly.eventsourcing.projection import Projection, ProjectionRunner
from pyfly.eventsourcing.snapshot import (
    InMemorySnapshotStore,
    Snapshot,
    SnapshotStore,
)
from pyfly.eventsourcing.store import (
    ConcurrencyError,
    EventStore,
    InMemoryEventStore,
    SqlAlchemyEventStore,
)
from pyfly.eventsourcing.upcaster import EventUpcaster, NoOpUpcaster

__all__ = [
    "AggregateRoot",
    "ConcurrencyError",
    "DomainEvent",
    "EventStore",
    "EventUpcaster",
    "InMemoryEventStore",
    "InMemorySnapshotStore",
    "NoOpUpcaster",
    "OutboxRecord",
    "Projection",
    "ProjectionRunner",
    "Snapshot",
    "SnapshotStore",
    "SqlAlchemyEventStore",
    "StoredEventEnvelope",
    "TransactionalOutbox",
    "domain_event",
]
