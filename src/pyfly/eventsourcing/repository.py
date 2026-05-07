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
"""Generic event-sourced repository: load, save, snapshot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar

from pyfly.eventsourcing.aggregate import AggregateRoot
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.snapshot import Snapshot, SnapshotStore
from pyfly.eventsourcing.store import EventStore

A = TypeVar("A", bound=AggregateRoot)


class EventSourcedRepository(Generic[A]):
    """Reconstructs aggregates from the event store and persists pending events."""

    def __init__(
        self,
        store: EventStore,
        factory: Callable[[], A],
        *,
        snapshots: SnapshotStore | None = None,
        snapshot_interval: int = 100,
    ) -> None:
        self._store = store
        self._factory = factory
        self._snapshots = snapshots
        self._snapshot_interval = snapshot_interval

    async def load(self, aggregate_id: str) -> A | None:
        aggregate = self._factory()
        aggregate.id = aggregate_id

        starting_seq = 0
        if self._snapshots is not None:
            snap = await self._snapshots.load(aggregate_id)
            if snap is not None:
                self._restore(aggregate, snap)
                starting_seq = snap.sequence

        events = await self._store.load(aggregate_id, after_sequence=starting_seq)
        if not events and starting_seq == 0:
            return None
        for envelope in events:
            payload_event = self._envelope_to_event(envelope)
            aggregate.replay(envelope.event_type, payload_event)
        return aggregate

    async def save(self, aggregate: A) -> None:
        pending = aggregate.pending_events()
        if not pending:
            return
        aggregate_type = type(aggregate).__name__
        envelopes = [
            StoredEventEnvelope.of(
                aggregate_id=aggregate.id,
                aggregate_type=aggregate_type,
                sequence=0,  # store will assign
                event=evt,
            )
            for evt in pending
        ]
        expected = aggregate.version - len(pending)
        await self._store.append(
            aggregate.id, aggregate_type, envelopes, expected_version=expected
        )
        aggregate.mark_committed()

        if self._snapshots is not None and aggregate.version % self._snapshot_interval == 0:
            await self._snapshots.save(
                Snapshot(
                    aggregate_id=aggregate.id,
                    aggregate_type=aggregate_type,
                    sequence=aggregate.version,
                    payload=self._dehydrate(aggregate),
                )
            )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _envelope_to_event(envelope: StoredEventEnvelope) -> object:
        # Build a simple object with attributes from the payload — concrete
        # repos can override to return a strongly-typed dataclass instance.
        obj = type(envelope.event_type, (), {})()
        for k, v in envelope.payload.items():
            setattr(obj, k, v)
        return obj

    @staticmethod
    def _dehydrate(aggregate: AggregateRoot) -> dict[str, object]:
        return {
            k: v
            for k, v in vars(aggregate).items()
            if not k.startswith("_") and k not in {"id", "version"}
        }

    @staticmethod
    def _restore(aggregate: AggregateRoot, snapshot: Snapshot) -> None:
        for k, v in snapshot.payload.items():
            setattr(aggregate, k, v)
        aggregate.version = snapshot.sequence
