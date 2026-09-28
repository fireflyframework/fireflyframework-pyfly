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
"""The transactional outbox as a layer over any transport: publish in the unit of work, forward after the commit.

Publishing to a broker from inside a unit of work is a dual write: the unit can roll back after the broker took
the event, and the process can die after the commit and before the publish. :class:`TransactionalEventPublisher`
removes it for any :class:`~pyfly.eda.ports.outbound.EventPublisher` transport (Kafka, RabbitMQ, Redis Streams,
the in-process bus):

- ``publish(destination, ...)`` appends the event to an outbox store (:class:`~pyfly.eda.ports.outbox.OutboxStore`:
  the SQL store, or any other adapter) in the caller's unit of work (the unit bound for the store's datasource;
  a short unit of its own outside one), owed to the forwarding relay's group, and wakes the relay once that unit
  commits. Nothing reaches the transport before the commit;
- :class:`OutboxForwarder`, an :class:`~pyfly.eda.outbox.OutboxRelay` of the group ``pyfly.forward:<transport>``,
  claims what the group is owed and publishes each event to the transport, outside every unit of work
  (:func:`~pyfly.data.transaction.outside_transaction`), with the relay's leases, retries, dead letters,
  retention and health;
- ``subscribe()`` and the consumption of events stay the transport's.

The guarantees:

- **A unit that rolls back publishes nothing.** Its event never existed.
- **A unit that commits publishes its event at least once.** In the normal path once; again when a publish failed
  (after the retry policy's back-off), and when the process died after the broker took the event and before the
  delivery was settled: the lease ends and a relay (this process after a restart, or another one) publishes it
  again. Every copy carries the event's id in the ``x-pyfly-event-id`` header (a header the publisher set, a
  domain event's id, is kept), so a consumer deduplicates on it.
- **Order.** The events a group is owed are claimed in publication order, and a claim is published in that order
  by one relay at a time, but a failed publish is attempted again after later events, and the relays of several
  processes claim side by side: consume events as independent facts, or order them yourself.
- **Several processes share the forwarding.** The processes whose forwarders run on one store with one group
  split the events between them (each is claimed by one relay at a time), so they must forward to the same
  transport; nothing is forwarded twice except after a lost lease (a crash, or a publish that outlasted
  ``claim_timeout``). An event that failed on every attempt stays in the store's dead letters.

The forwarder's group is registered for its destinations (every one by default), so an event another writer of
the store appends to one of them is forwarded too. A publish to a destination the forwarder does not take goes
straight to the transport, after the caller's unit commits (at once outside one): at most once, and lost when the
process dies in between.

``pyfly.eda.outbox.enabled`` wraps the configured provider's publisher (see
:mod:`pyfly.eda.auto_configuration`); the ``database`` and ``postgres`` providers are the outbox already.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox import OutboxRelay, describe_error
from pyfly.eda.ports.outbound import EventHandler, EventPublisher
from pyfly.eda.ports.outbox import OutboxStore, Retention, StartPosition
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthStatus
    from pyfly.eda.dlq import EdaDeadLetterEntry, EdaDeadLetterStore
    from pyfly.eda.ports.outbox import PendingDelivery
    from pyfly.messaging.listener_container import ListenerContainerSettings, RetryPolicy

__all__ = [
    "FORWARD_GROUP_PREFIX",
    "OutboxForwarder",
    "PublisherState",
    "TransactionalEventPublisher",
    "forward_group",
]

_logger = logging.getLogger(__name__)

FORWARD_GROUP_PREFIX = "pyfly.forward:"
"""The consumer group of a forwarder is ``pyfly.forward:<transport>`` unless it is given another."""


def forward_group(transport: str) -> str:
    """The consumer group of the forwarder to the transport named *transport* (``kafka``, ``rabbitmq``...)."""
    return f"{FORWARD_GROUP_PREFIX}{transport}"


class OutboxForwarder(OutboxRelay):
    """The relay that publishes what an outbox store owes its group to a transport (see the module documentation).

    - *store*: the outbox store; *transport*: the :class:`~pyfly.eda.ports.outbound.EventPublisher` the events
      are published to;
    - *name*: the transport's name (``kafka``, ``rabbitmq``...; by default its class name), and *group* the
      consumer group (by default ``pyfly.forward:<name>``). The processes of one group share its events: give
      forwarders to different transports different groups;
    - *destinations*: the destinations it forwards (``None``: every one); the group is registered for them,
      starting at *start_position*;
    - *retry*, *error_strategy*, *dead_letter_store*, *poll_interval*, *batch_size*, *claim_timeout*,
      *handler_timeout* (the longest one publish may take), *retention* (``None``: the default
      :class:`~pyfly.eda.ports.outbox.Retention`), *settings* (its shutdown timeout) and *owner*: see
      :class:`~pyfly.eda.outbox.OutboxRelay`.

    Its one subscription publishes every claimed event to the transport, outside any unit of work, with the
    event's id in the ``x-pyfly-event-id`` header; a publish that raises (or outlasts *handler_timeout*) is a
    failed attempt.
    """

    def __init__(
        self,
        store: OutboxStore,
        transport: EventPublisher,
        *,
        name: str | None = None,
        group: str | None = None,
        destinations: Sequence[str] | None = None,
        start_position: StartPosition | str = StartPosition.LATEST,
        retry: RetryPolicy | None = None,
        error_strategy: ErrorStrategy = ErrorStrategy.DEAD_LETTER,
        settings: ListenerContainerSettings | None = None,
        poll_interval: float = 5.0,
        batch_size: int = 100,
        claim_timeout: float = 300.0,
        handler_timeout: float | None = 60.0,
        retention: Retention | None = None,
        dead_letter_store: EdaDeadLetterStore | None = None,
        owner: str | None = None,
    ) -> None:
        self._transport_name = name or type(transport).__name__
        group = group or forward_group(self._transport_name)
        super().__init__(
            store,
            group=group,
            destinations=destinations,
            register=True,
            start_position=start_position,
            retry=retry,
            error_strategy=error_strategy,
            settings=settings,
            transactional=False,  # a publish is not database work: no unit of work around it
            poll_interval=poll_interval,
            batch_size=batch_size,
            claim_timeout=claim_timeout,
            handler_timeout=handler_timeout,
            retention=retention if retention is not None else Retention(),
            dead_letter_store=dead_letter_store,
            owner=owner,
            name=f"outbox-forwarder {group}",
        )
        self._transport = transport
        self.subscribe("*", self._forward)

    @property
    def transport(self) -> EventPublisher:
        """The transport the events are published to."""
        return self._transport

    @property
    def transport_name(self) -> str:
        """The transport's name, as the group is named after it."""
        return self._transport_name

    def forwards(self, destination: str) -> bool:
        """Whether the forwarder takes the events of *destination*."""
        destinations = self.destinations
        return destinations is None or destination in destinations

    async def _forward(self, envelope: EventEnvelope) -> None:
        """Publish one claimed event to the transport, outside the units of work of the task that runs it."""
        from pyfly.data.transaction import outside_transaction

        headers = {EVENT_ID_HEADER: envelope.event_id, **envelope.headers}
        with outside_transaction():
            await self._transport.publish(envelope.destination, envelope.event_type, envelope.payload, headers)


class PublisherState(enum.Enum):
    """Where a :class:`TransactionalEventPublisher` is in its lifecycle."""

    NEW = "new"
    RUNNING = "running"
    STOPPED = "stopped"


class TransactionalEventPublisher:
    """An :class:`~pyfly.eda.ports.outbound.EventPublisher` that publishes to *transport* through the outbox
    *store* (see the module documentation).

    *forwarder* is the relay that forwards the events; without one, the publisher builds an
    :class:`OutboxForwarder` of *store* and *transport* with *options* (``name``, ``group``, ``destinations``,
    ``retry``, ``poll_interval``...). :meth:`start` starts the transport, the store and the forwarder, and
    :meth:`stop` stops the forwarder first (the publish in flight finishes), then the transport and the store.
    A publish never starts anything: after :meth:`stop` the event is still appended, for a later relay.
    """

    #: The publisher consumes (the transport's subscriptions, the forwarder's claims): it stops before any
    #: ``@pre_destroy``.
    phase = CONSUMER_PHASE
    #: A publish joins the caller's unit of work (the CQRS and domain-event publishers rely on it): the unit bound
    #: for the store's datasource, so the event commits with the business changes when the store is on their
    #: datasource (on another one it commits in a unit of that datasource).
    joins_transactions = True

    def __init__(
        self,
        transport: EventPublisher,
        store: OutboxStore,
        *,
        forwarder: OutboxForwarder | None = None,
        **options: Any,
    ) -> None:
        if forwarder is None:
            forwarder = OutboxForwarder(store, transport, **options)
        elif options:
            raise TypeError(f"forwarder options {sorted(options)} apply only when the publisher builds its forwarder")
        elif forwarder.outbox is not store or forwarder.transport is not transport:
            raise ValueError("the forwarder must forward from this store to this transport")
        self._transport = transport
        self._store = store
        self._forwarder = forwarder
        self._state = PublisherState.NEW
        self._lock = asyncio.Lock()

    # -- introspection ---------------------------------------------------------------------------------------------

    @property
    def transport(self) -> EventPublisher:
        """The transport the events are forwarded to (and consumed from)."""
        return self._transport

    @property
    def store(self) -> OutboxStore:
        """The outbox store the events are appended to."""
        return self._store

    @property
    def forwarder(self) -> OutboxForwarder:
        """The relay that forwards them."""
        return self._forwarder

    @property
    def relay(self) -> OutboxForwarder:
        """The forwarder, under the name the outbox buses give their relay (a data test runs
        ``await publisher.relay.run_once()`` to forward what it published)."""
        return self._forwarder

    @property
    def group(self) -> str:
        """The forwarder's consumer group."""
        return self._forwarder.group

    @property
    def state(self) -> PublisherState:
        """Where the publisher is in its lifecycle."""
        return self._state

    @property
    def running(self) -> bool:
        """Whether the publisher is started."""
        return self._state is PublisherState.RUNNING

    @property
    def manages_listener_errors(self) -> bool:
        """Whether the transport retries and dead-letters its listeners' failures itself."""
        return getattr(self._transport, "manages_listener_errors", False) is True

    # -- EventPublisher ------------------------------------------------------------------------------------------

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        """Subscribe *handler* on the transport: consumption is the transport's."""
        self._transport.subscribe(event_type_pattern, handler)

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Append the event to the outbox store in the unit of work bound for its datasource (or a short unit of
        its own), owed to the forwarder's group; the forwarder publishes it to the transport once that unit
        committed. A destination the forwarder does not take is published to the transport after the commit."""
        if not self._forwarder.forwards(destination):
            await self._publish_after_commit(destination, event_type, payload, headers)
            return
        envelope = EventEnvelope(event_type=event_type, payload=payload, destination=destination, headers=headers or {})
        await self._store.append(envelope, include=(self._forwarder.group,))
        self._forwarder.wake_after_commit()

    async def _publish_after_commit(
        self, destination: str, event_type: str, payload: dict[str, Any], headers: dict[str, str] | None
    ) -> None:
        from pyfly.data.transaction import after_commit

        async def send() -> None:
            await self._transport.publish(destination, event_type, payload, headers)

        await after_commit(send)

    # -- lifecycle -----------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the transport, the store (its tables) and the forwarder, which registers its group. Serialized
        and idempotent; when a step fails, what the earlier ones started is stopped again and the failure is
        raised."""
        async with self._lock:
            if self._state is PublisherState.RUNNING:
                return
            try:
                await self._transport.start()
                await self._store.start()
                await self._forwarder.register()
                await self._forwarder.start()
            except BaseException:
                with contextlib.suppress(Exception):  # the failure that stopped the start is the one raised
                    await self._release()
                raise
            self._state = PublisherState.RUNNING
            _logger.info(
                "eda_transactional_publisher_started",
                extra={
                    "transport": type(self._transport).__name__,
                    "store": type(self._store).__name__,
                    "group": self.group,
                    "destinations": self._forwarder.destinations,
                },
            )

    async def stop(self) -> None:
        """Stop the forwarder (the publish in flight finishes), then the transport and the store. Idempotent."""
        async with self._lock:
            if self._state is not PublisherState.RUNNING:
                return
            try:
                await self._release()
            finally:
                self._state = PublisherState.STOPPED

    async def _release(self) -> None:
        """Stop what runs, the forwarder first: it publishes to the transport."""
        try:
            await self._forwarder.stop()
        finally:
            try:
                await self._transport.stop()
            finally:
                with contextlib.suppress(Exception):
                    await self._store.stop()

    # -- reading ---------------------------------------------------------------------------------------------

    async def pending(self, *, limit: int = 1000) -> list[PendingDelivery]:
        """The events not forwarded yet (claimed ones included), oldest first."""
        return await self._store.pending(self.group, limit=limit)

    async def dead_letters(self, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        """The events the forwarder failed to publish on every attempt, most recent first."""
        return await self._store.dead_letters(self.group, limit=limit)

    # -- health ------------------------------------------------------------------------------------------------

    async def health_status(self) -> HealthStatus:
        """``UP`` while the publisher runs, its forwarder's task runs, its store answers (when the store has a
        ``ping()``) and its transport is up; ``DOWN`` otherwise, with the reason. The details count what was
        forwarded, the failed attempts and the dead letters."""
        from pyfly.actuator.health import HealthStatus
        from pyfly.eda.health import EventPublisherHealthIndicator

        counters = self._forwarder.counters
        details: dict[str, Any] = {
            "adapter": type(self).__name__,
            "transport": type(self._transport).__name__,
            "store": type(self._store).__name__,
            "group": self.group,
            "forwarded": counters.delivered,
            "failures": counters.failures,
            "dead_lettered": counters.dead_lettered,
        }
        if self._state is not PublisherState.RUNNING:
            return HealthStatus(status="DOWN", details={**details, "reason": f"publisher {self._state.value}"})
        if not self._forwarder.alive:
            # Nothing is forwarded from this process any more (its events wait for another one, or a restart).
            return HealthStatus(
                status="DOWN",
                details={**details, "reason": "the forwarder's task ended", "error": self._forwarder.last_error},
            )
        ping = getattr(self._store, "ping", None)
        if callable(ping):
            try:
                await asyncio.wait_for(ping(), timeout=2.0)
            except Exception as error:  # noqa: BLE001 — reported, not raised
                return HealthStatus(
                    status="DOWN", details={**details, "reason": "the store", "error": describe_error(error)[:200]}
                )
        transport = await EventPublisherHealthIndicator(self._transport).health()
        if transport.status != "UP":
            return HealthStatus(
                status="DOWN", details={**details, "reason": "the transport", "transport_details": transport.details}
            )
        if self._forwarder.last_error is not None:
            details["last_round_error"] = self._forwarder.last_error
        return HealthStatus(status="UP", details=details)
