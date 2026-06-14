# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Bridge from drained domain events to the EDA bus.

The :class:`Wallet` aggregate raises :class:`~pyfly.domain.DomainEvent`
instances during a command. After persisting, the handler drains them
with ``wallet.clear_events()`` and hands each one here, which translates
it into the dict payload the :class:`~pyfly.eda.EventPublisher` expects
and publishes it on the wallet events channel.

Keeping this in one place means every command handler publishes events
identically, and the listener
(:class:`~lumen.core.services.listeners.wallet_audit_listener.WalletAuditListener`)
sees a consistent payload shape.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from lumen.core.services.listeners.wallet_audit_listener import (
    WALLET_EVENTS_DESTINATION,
)
from pyfly.domain import DomainEvent
from pyfly.eda import EventPublisher


def _to_payload(event: DomainEvent) -> dict[str, Any]:
    """Flatten a frozen-dataclass domain event into a JSON-friendly dict."""
    payload: dict[str, Any] = dataclasses.asdict(event)
    # event_type is a computed property (the class name), not a field —
    # include it so consumers can read it from the payload too.
    payload.setdefault("event_type", event.event_type)
    return payload


async def publish_domain_events(publisher: EventPublisher, events: Iterable[DomainEvent]) -> None:
    """Publish each drained domain event on the wallet events channel.

    The envelope's ``event_type`` is the domain event class name
    (``WalletOpened`` / ``FundsDeposited`` / ``FundsWithdrawn``), which is
    exactly what listeners subscribe to.
    """
    for event in events:
        await publisher.publish(
            destination=WALLET_EVENTS_DESTINATION,
            event_type=event.event_type,
            payload=_to_payload(event),
        )
