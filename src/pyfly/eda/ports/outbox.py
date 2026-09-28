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
"""The ``OutboxStore`` port: where the transactional outbox keeps its events, and how they are claimed.

An outbox store holds events written in the unit of work of the code that publishes them, and what it still
owes each consumer group. The relay (:class:`~pyfly.eda.outbox.OutboxRelay`) claims what a group is owed, hands
it to the group's subscriptions and settles it; the buses built on it (the ``database`` and ``postgres`` event
buses, the forwarding relay that makes any broker transactional, the event-sourcing ``TransactionalOutbox``)
type against this port only, never against an adapter:

- :class:`~pyfly.eda.outbox.SqlOutboxStore` keeps them in four framework tables on any SQL datasource;
- a document store (MongoDB) implements the same port on its collections.

What a store must do is the contract suite (``tests/support/outbox_contract.py``), run on every backend an
adapter supports. In short:

- :meth:`OutboxStore.append` joins the unit of work bound for the store's datasource (or runs in a short unit of
  its own): a rolled-back unit leaves nothing, a committed one leaves the event, owed to every group registered
  for its destination (or to the groups named), and to the groups included, each once;
- :meth:`OutboxStore.claim` takes the deliveries of a group that are due, in publication order, for a lease: a
  delivery another claim holds is never taken, and one whose lease ended is taken again. Every later statement
  on a claim (:meth:`~OutboxStore.complete`, :meth:`~OutboxStore.settle`, :meth:`~OutboxStore.extend`,
  :meth:`~OutboxStore.release`) is fenced by it: once another claim took the delivery, it does nothing;
- :meth:`OutboxStore.register` makes a group's destinations exactly the ones given, and a new group starts at
  the latest event or (``earliest``) with the events the store holds for them;
- :meth:`OutboxStore.prune` deletes the events every group handled, and those past a maximum age.

The outbox id of an event is an integer that grows with each append: it is the publication order a claim
follows. Instants are aware ``datetime`` values in UTC, from the store's clock (:meth:`OutboxStore.now`).

A store may define ``async def ping() -> None`` beside the port (the SQL store does): the health indicators of
the buses call it, when it is there, to report the store's backend.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope

if TYPE_CHECKING:
    from pyfly.eda.dlq import EdaDeadLetterEntry

__all__ = [
    "ADDRESSED_DESTINATION_PREFIX",
    "EVERY_DESTINATION",
    "Delivery",
    "OutboxStore",
    "PendingDelivery",
    "PruneResult",
    "Retention",
    "StartPosition",
]

EVERY_DESTINATION = "*"
"""The destination a consumer group registers to receive the events of every destination."""

ADDRESSED_DESTINATION_PREFIX = "eventsourcing.outbox:"
"""The destinations of the events owed only to the groups their append names (the event-sourcing
:class:`~pyfly.eventsourcing.outbox.TransactionalOutbox` appends to ``eventsourcing.outbox:<name>``): a group
registered for every destination is not owed them, nor given them when it starts at the earliest event."""


class StartPosition(enum.Enum):
    """Where a consumer group that registers for the first time starts."""

    LATEST = "latest"
    """With the events published after it registered (what a new Kafka, Redis or RabbitMQ consumer gets)."""
    EARLIEST = "earliest"
    """With every event the outbox still holds for its destinations, then the ones published after. An event
    whose publishing unit is in flight while the group registers can be missed: its publish read the groups
    before the registration committed, and the registration read the events before the publish committed."""

    @classmethod
    def of(cls, value: StartPosition | str) -> StartPosition:
        """*value* as a start position (``latest`` or ``earliest``, any case)."""
        if isinstance(value, StartPosition):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise ValueError(f"A start position is 'latest' or 'earliest', got {value!r}") from None


@dataclass(frozen=True)
class Retention:
    """How long the outbox keeps events.

    - *delivered*: an event every group has handled is deleted once it is older than this (``None``: never).
      The sweep reads past the events some group still owes: an abandoned group's backlog is read again at
      every sweep (unregister the group, or set *max_age*);
    - *max_age*: an event older than this is deleted with the deliveries still owed for it, whether or not
      they were made (``None``: never; a group that stops consuming then keeps its events);
    - *interval*: how often a relay sweeps, and *batch_size* how many events one statement deletes.
    """

    delivered: timedelta | None = timedelta(hours=1)
    max_age: timedelta | None = None
    interval: timedelta = timedelta(minutes=1)
    batch_size: int = 1000

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"Retention.batch_size must be at least 1, got {self.batch_size}")
        for name in ("delivered", "max_age"):
            value = getattr(self, name)
            if value is not None and value < timedelta(0):
                raise ValueError(f"Retention.{name} must not be negative, got {value}")


@dataclass(frozen=True)
class PruneResult:
    """What one retention sweep deleted: events every group had handled, and events past their maximum age
    (with *undelivered* deliveries still owed for them)."""

    delivered: int = 0
    expired: int = 0
    undelivered: int = 0


@dataclass(frozen=True)
class Delivery:
    """A delivery a relay claimed: the event, which attempt this is, and the subscriptions that handled it
    already. *token* identifies the claim; settling the delivery needs it. *leased_until* is when the claim's
    lease ends, and *due_at* when the delivery was due before it was claimed (a release gives it back there)."""

    outbox_id: int
    group: str
    envelope: EventEnvelope
    attempts: int
    done: frozenset[str]
    token: str
    last_error: str | None = None
    leased_until: datetime | None = None
    due_at: datetime | None = None


@dataclass(frozen=True)
class PendingDelivery:
    """A delivery the outbox still owes a group, as :meth:`OutboxStore.pending` reads it."""

    outbox_id: int
    envelope: EventEnvelope
    attempts: int
    available_at: datetime
    last_error: str | None


@runtime_checkable
class OutboxStore(Protocol):
    """Where the transactional outbox keeps its events and deliveries (see the module documentation).

    :class:`~pyfly.eda.outbox.SqlOutboxStore` is the SQL adapter; its documentation describes each method in
    full, and every adapter behaves the same way (the contract suite proves it).
    """

    async def start(self) -> None:
        """Make the store ready (create or check what it keeps its data in). Idempotent."""
        ...

    async def stop(self) -> None:
        """Release what :meth:`start` acquired. Idempotent; the data stays."""
        ...

    def now(self) -> datetime:
        """The store's clock: the current instant, aware and in UTC."""
        ...

    async def append(
        self, envelope: EventEnvelope, *, groups: Sequence[str] | None = None, include: Sequence[str] = ()
    ) -> int:
        """Write *envelope* in the unit of work bound for the store's datasource (or a short unit of its own),
        owed to every group registered for its destination (or to *groups* when given, then no registration is
        consulted) and to the groups in *include*, each once. Returns its outbox id."""
        ...

    async def register(
        self,
        group: str,
        destinations: Sequence[str] | None,
        *,
        start: StartPosition | str = StartPosition.LATEST,
    ) -> bool:
        """Make *destinations* (``None``: every destination) exactly the ones *group* is owed, in a unit of its
        own; a new group starts at *start*. Returns whether the group was new."""
        ...

    async def unregister(self, group: str) -> int:
        """Remove *group* and what it was still owed; returns how many deliveries were dropped."""
        ...

    async def claim(self, group: str, *, limit: int, lease: timedelta, owner: str) -> list[Delivery]:
        """Claim up to *limit* deliveries owed to *group* whose time has come, for *lease*, in publication
        order."""
        ...

    async def complete(self, deliveries: Sequence[Delivery]) -> int:
        """Settle claimed deliveries every subscription handled; returns how many were still held."""
        ...

    async def settle(
        self,
        delivery: Delivery,
        *,
        done: Iterable[str] = (),
        retry_at: datetime | None = None,
        error: str | None = None,
        dead: Sequence[tuple[str, BaseException]] = (),
    ) -> bool:
        """Record the subscriptions that handled a claimed delivery (*done*), the ones that go to the dead
        letters (*dead*), and either its next attempt at *retry_at* or its end. Returns ``False``, recording
        nothing, when another claim took it since."""
        ...

    async def extend(self, deliveries: Sequence[Delivery], *, until: datetime) -> list[Delivery]:
        """Extend the lease of claimed deliveries to *until*; returns the ones still held, with their lease."""
        ...

    async def release(self, deliveries: Sequence[Delivery]) -> int:
        """Give claimed deliveries back unattempted, due when they were before the claim; returns how many."""
        ...

    async def pending(self, group: str, *, limit: int = 1000) -> list[PendingDelivery]:
        """The deliveries still owed to *group* (claimed ones included), oldest first."""
        ...

    async def dead_letters(self, group: str | None = None, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        """The dead letters (of *group*, or of every group), most recent first."""
        ...

    async def prune(self, retention: Retention) -> PruneResult:
        """Delete what *retention* lets go, in batches."""
        ...
