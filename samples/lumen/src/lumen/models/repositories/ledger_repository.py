# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Event-sourced repository for the :class:`LedgerAccount` aggregate.

This is the persistence boundary for the ledger. It does not store rows —
it appends each aggregate's pending events to an
:class:`~pyfly.eventsourcing.EventStore` and rebuilds the aggregate by
**replaying** that stream. The heavy lifting lives in PyFly's generic
:class:`~pyfly.eventsourcing.repository.EventSourcedRepository`:

* :meth:`save` drains ``aggregate.pending_events()``, wraps each in a
  :class:`~pyfly.eventsourcing.StoredEventEnvelope`, and ``append``\\ s the
  batch with optimistic concurrency (``expected_version``).
* :meth:`load` reads the stored envelopes back, turns each into an event
  object, and folds them through the aggregate's ``replay`` handlers to
  reconstruct current state. Optional snapshots truncate replay cost.

We subclass it only to give the ledger a *typed* replay: the base class
hydrates a generic attribute-bag from each stored payload, which works,
but rebuilding the real ``LedgerOpened`` / ``Credited`` / ``Debited``
dataclass is cleaner and lets the aggregate's handlers stay strongly
typed.
"""

from __future__ import annotations

from typing import ClassVar

from lumen.models.entities.v1.ledger_account import (
    Credited,
    Debited,
    LedgerAccount,
    LedgerOpened,
)
from pyfly.eventsourcing import DomainEvent, EventStore, SnapshotStore, StoredEventEnvelope
from pyfly.eventsourcing.repository import EventSourcedRepository

# Map a stored ``event_type`` (the event class name) back to its dataclass.
_EVENT_TYPES: dict[str, type[DomainEvent]] = {
    LedgerOpened.__name__: LedgerOpened,
    Credited.__name__: Credited,
    Debited.__name__: Debited,
}


class LedgerAccountRepository(EventSourcedRepository[LedgerAccount]):
    """Loads/saves :class:`LedgerAccount` aggregates via the event store."""

    # The ledger's stream is short (a handful of events), so snapshots are
    # not needed for correctness — wire one anyway to show the seam and to
    # exercise the snapshot path under load. 100 = the framework default.
    SNAPSHOT_INTERVAL: ClassVar[int] = 100

    def __init__(self, store: EventStore, *, snapshots: SnapshotStore | None = None) -> None:
        super().__init__(
            store,
            factory=LedgerAccount,
            snapshots=snapshots,
            snapshot_interval=self.SNAPSHOT_INTERVAL,
        )

    @staticmethod
    def _envelope_to_event(envelope: StoredEventEnvelope) -> object:
        """Rebuild the concrete event dataclass from a stored payload.

        Overrides the base class's generic attribute-bag so that replayed
        events are the same dataclasses the aggregate applied on the write
        side. Unknown fields are ignored so the ledger keeps replaying even
        after an event grows new fields.
        """
        event_cls = _EVENT_TYPES.get(envelope.event_type)
        if event_cls is None:
            # Fall back to the framework's generic hydration for any event
            # type we don't recognise (forward-compatibility).
            return EventSourcedRepository._envelope_to_event(envelope)
        field_names = {f.name for f in event_cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in envelope.payload.items() if k in field_names}
        return event_cls(**kwargs)
