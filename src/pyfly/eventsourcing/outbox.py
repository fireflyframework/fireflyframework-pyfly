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
"""Transactional outbox — at-least-once delivery of stored events to a broker.

:meth:`TransactionalOutbox.enqueue` writes the event into the framework's outbox table
(:mod:`pyfly.eda.outbox`) in the unit of work bound for the outbox's datasource: enqueued inside the unit that
appends the events or saves the aggregate, it is there exactly when that unit commits, and a rollback takes
it back. A relay, started with the application context (a :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE`
lifecycle bean), hands every committed event to *publish*, attempts a failed one again after a back-off, and
after *max_attempts* keeps it in the dead-letter table (:meth:`TransactionalOutbox.dead_letters`). Delivered
events are pruned. Several processes may run the same outbox: each event is claimed by one of them.

The outbox reads and writes through the :class:`~pyfly.eda.ports.outbox.OutboxStore` port: by default a
:class:`~pyfly.eda.outbox.SqlOutboxStore` on the datasource it is given, or any store passed as *store*.

Before 26.09.08 the outbox was a dictionary in the process: an event enqueued by a unit that then rolled
back was published anyway, a restart lost everything pending, and nothing was ever removed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pyfly.eda.outbox import OutboxRelay, OutboxTables, SqlOutboxStore
from pyfly.eda.ports.outbox import ADDRESSED_DESTINATION_PREFIX, OutboxStore, Retention
from pyfly.eda.types import EventEnvelope
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE

if TYPE_CHECKING:
    from pyfly.messaging.listener_container import BackOff

GROUP_PREFIX = ADDRESSED_DESTINATION_PREFIX
"""The consumer group of an outbox named *name* is ``eventsourcing.outbox:<name>``, and so is the destination of
its events: they are owed to that group alone (an EDA group registered for every destination never gets them)."""


@dataclass
class OutboxRecord:
    """One outbox delivery, as :meth:`TransactionalOutbox.pending` and :meth:`TransactionalOutbox.dead_letters`
    read it: *attempts* made so far, *last_error* the last failure.

    *delivered* is kept for compatibility and is always ``False``: a delivered event leaves the pending
    deliveries (and a dead-lettered one was never delivered), so neither listing holds a delivered record."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event: StoredEventEnvelope = field(default_factory=StoredEventEnvelope)
    attempts: int = 0
    delivered: bool = False
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TransactionalOutbox:
    """Stores events in the outbox table; a relay forwards them to the broker (see the module documentation).

    Args:
        publish: async callable that publishes a single envelope. It raises on failure, so the outbox attempts
            the event again.
        datasource: where the outbox table lives: a datasource name, a registry ``DataSource``, an
            ``AsyncEngine``, or ``None`` (the default datasource).
        store: the outbox store to run on instead (an :class:`~pyfly.eda.ports.outbox.OutboxStore`; then
            neither *datasource*, *tables* nor *create_tables* applies).
        name: the outbox's name; outboxes with different names on one datasource deliver apart.
        max_attempts: the attempts before an event goes to the dead letters.
        poll_interval_s: how often the relay looks for events enqueued by other processes (an enqueue in this
            process wakes it when its unit commits).
        backoff: the delay before the next attempt of a failed event (by default exponential, from 1 s to 30 s).
        publish_timeout: seconds a publish may take before it counts as failed (``None``: no limit).
        create_tables: create the outbox tables when they are missing (otherwise they are only checked).
        retention: how long delivered events are kept.
    """

    #: The relay stops before any ``@pre_destroy``, and before the publisher it relays through (a
    #: consumer created before it), so it never publishes into a stopped or destroyed bean
    #: (see :mod:`pyfly.kernel.lifecycle`).
    phase = CONSUMER_PHASE

    def __init__(
        self,
        publish: Callable[[StoredEventEnvelope], Awaitable[None]],
        *,
        datasource: object = None,
        name: str = "default",
        max_attempts: int = 5,
        poll_interval_s: float = 1.0,
        backoff: BackOff | None = None,
        publish_timeout: float | None = 60.0,
        create_tables: bool = True,
        tables: OutboxTables | None = None,
        retention: Retention | None = None,
        owner: str | None = None,
        store: OutboxStore | None = None,
    ) -> None:
        from pyfly.messaging.listener_container import ExponentialBackOff, RetryPolicy

        if store is None:
            store = SqlOutboxStore(datasource, tables=tables, create_tables=create_tables)
        elif datasource is not None or tables is not None:
            raise ValueError("TransactionalOutbox takes a store, or a datasource (and tables) to build one, not both")
        self._publish = publish
        self._group = f"{GROUP_PREFIX}{name}"
        self._outbox: OutboxStore = store
        self._relay = OutboxRelay(
            self._outbox,
            group=self._group,
            register=False,  # an enqueue names the group: no registration is consulted
            retry=RetryPolicy(max_attempts=max_attempts, backoff=backoff or ExponentialBackOff()),
            transactional=False,
            poll_interval=poll_interval_s,
            handler_timeout=publish_timeout,
            retention=retention if retention is not None else Retention(),
            owner=owner,
            name=f"transactional-outbox {name}",
        )
        self._relay.subscribe("*", self._forward)

    @property
    def outbox(self) -> OutboxStore:
        """The outbox store the events are written to."""
        return self._outbox

    @property
    def relay(self) -> OutboxRelay:
        """The relay that publishes them."""
        return self._relay

    @property
    def group(self) -> str:
        """The consumer group the outbox's events are owed to."""
        return self._group

    async def enqueue(self, event: StoredEventEnvelope) -> OutboxRecord:
        """Write *event* into the outbox, in the unit of work bound for the outbox's datasource (or a short unit
        of its own); the relay publishes it once that unit commits."""
        envelope = EventEnvelope(
            event_type=event.event_type,
            payload=json.loads(event.to_json()),
            destination=self._group,
        )
        await self._outbox.append(envelope, groups=[self._group])
        self._relay.wake_after_commit()
        return OutboxRecord(id=envelope.event_id, event=event, created_at=envelope.timestamp)

    async def _forward(self, envelope: EventEnvelope) -> None:
        await self._publish(StoredEventEnvelope.from_json(json.dumps(envelope.payload)))

    async def start(self) -> None:
        """Start the outbox store (check and create its tables) and the relay. Idempotent."""
        await self._outbox.start()
        await self._relay.start()

    async def stop(self) -> None:
        """Stop the relay, then the store (also when the relay's stop raised); what it had not delivered stays in
        the store. Idempotent."""
        try:
            await self._relay.stop()
        finally:
            await self._outbox.stop()

    async def pending(self) -> list[OutboxRecord]:
        """The events not delivered yet and still to be attempted (a dead-lettered one is not pending)."""
        return [
            OutboxRecord(
                id=delivery.envelope.event_id,
                event=_stored(delivery.envelope.payload),
                attempts=delivery.attempts,
                last_error=delivery.last_error,
                created_at=delivery.envelope.timestamp,
            )
            for delivery in await self._outbox.pending(self._group)
        ]

    async def dead_letters(self) -> list[OutboxRecord]:
        """The events that failed on every attempt, kept for inspection or a manual retry, most recent first.

        At-least-once delivery holds up to ``max_attempts``; :meth:`pending` excludes these, so the relay stops
        attempting them.
        """
        return [
            OutboxRecord(
                id=letter.event.event_id,
                event=_stored(letter.event.payload),
                attempts=letter.attempts,
                last_error=f"{letter.error_type}: {letter.error_message}",
                created_at=letter.event.timestamp,
            )
            for letter in await self._outbox.dead_letters(self._group, limit=1000)
        ]


def _stored(payload: dict[str, Any]) -> StoredEventEnvelope:
    return StoredEventEnvelope.from_json(json.dumps(payload))
