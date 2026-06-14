# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""A domain-event listener — the EDA consumer side (Chapter 8).

When a wallet command runs, the handler drains the aggregate's pending
:class:`~pyfly.domain.DomainEvent` instances and **publishes** them on the
:class:`~pyfly.eda.EventPublisher` bus (see the command handlers). This
listener subscribes to those events and maintains two read-side
projections entirely in memory:

* an **audit log** — one :class:`AuditEntry` per event, in order;
* a **running total** of net deposited funds per wallet (minor units).

It is a plain ``@service`` whose handler method is annotated with
``@event_listener``. During ``ApplicationContext`` startup PyFly discovers
the stamped method and auto-subscribes it to the ``EventPublisher`` bean —
no bus reference is wired by hand. The handler receives an
:class:`~pyfly.eda.EventEnvelope`; its ``event_type`` is the domain event
class name (``WalletOpened`` / ``FundsDeposited`` / ``FundsWithdrawn``) and
its ``payload`` is the event's fields as a dict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener

logger = logging.getLogger(__name__)

# The logical channel the wallet handlers publish domain events to.
WALLET_EVENTS_DESTINATION = "wallet.events"


@dataclass(frozen=True)
class AuditEntry:
    """One observed domain event, captured for the audit trail."""

    event_type: str
    wallet_id: str
    event_id: str
    occurred_at: datetime
    payload: dict[str, object]


@service
class WalletAuditListener:
    """In-memory audit log + running-total projection over wallet events."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        # Net deposited minus withdrawn, per wallet, in minor units.
        self._running_totals: dict[str, int] = {}

    @event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])
    async def on_wallet_event(self, envelope: EventEnvelope) -> None:
        """Project every wallet domain event into the read models."""
        payload = dict(envelope.payload)
        wallet_id = str(payload.get("wallet_id", ""))

        self._entries.append(
            AuditEntry(
                event_type=envelope.event_type,
                wallet_id=wallet_id,
                event_id=str(payload.get("event_id", envelope.event_id)),
                occurred_at=envelope.timestamp,
                payload=payload,
            )
        )

        if envelope.event_type == "WalletOpened":
            self._running_totals.setdefault(wallet_id, 0)
        elif envelope.event_type == "FundsDeposited":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = self._running_totals.get(wallet_id, 0) + amount
        elif envelope.event_type == "FundsWithdrawn":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = self._running_totals.get(wallet_id, 0) - amount

        logger.info(
            "wallet_audit_observed",
            extra={"event_type": envelope.event_type, "wallet_id": wallet_id},
        )

    # --- read-side accessors --------------------------------------------

    @property
    def entries(self) -> list[AuditEntry]:
        """A snapshot of the audit log, in observation order."""
        return list(self._entries)

    def entries_for(self, wallet_id: str) -> list[AuditEntry]:
        """The audit entries recorded for one wallet."""
        return [e for e in self._entries if e.wallet_id == wallet_id]

    def running_total(self, wallet_id: str) -> int:
        """Net funds (deposited − withdrawn) for *wallet_id*, minor units."""
        return self._running_totals.get(wallet_id, 0)
