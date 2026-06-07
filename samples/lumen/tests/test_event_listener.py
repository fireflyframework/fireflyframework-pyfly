# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Feature 2 — the wallet domain-event listener (Chapter 8, EDA).

Proves the real publish path end to end:

1. A command runs on the CQRS bus.
2. Its handler drains the aggregate's domain events
   (``wallet.clear_events()``) and publishes them on the EDA
   ``EventPublisher`` bus.
3. The ``@event_listener`` audit projection — subscribed to the bus the
   same way the ApplicationContext auto-wires it — observes every event.

The ``command_bus`` and ``audit_listener`` fixtures share one
``InMemoryEventBus`` (see ``conftest.py``), so this is the production code
path, not a mock.
"""

from __future__ import annotations

import pytest
from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus


@pytest.mark.asyncio
async def test_listener_observes_wallet_events(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    wallet_id = await command_bus.send(OpenWallet(owner_id="u-1", currency=Currency.EUR))
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=400))

    entries = audit_listener.entries_for(wallet_id)
    assert [e.event_type for e in entries] == [
        "WalletOpened",
        "FundsDeposited",
        "FundsWithdrawn",
    ]

    # The payload carried the real domain-event fields.
    deposited = entries[1]
    assert deposited.payload["amount"] == 1500
    assert deposited.payload["currency"] == "EUR"
    assert deposited.payload["balance"] == 1500
    assert deposited.event_id  # the aggregate's DomainEvent.event_id

    # The running-total projection reflects deposit − withdrawal.
    assert audit_listener.running_total(wallet_id) == 1100


@pytest.mark.asyncio
async def test_listener_records_nothing_before_any_command(
    audit_listener: WalletAuditListener,
) -> None:
    assert audit_listener.entries == []
    assert audit_listener.running_total("anything") == 0


@pytest.mark.asyncio
async def test_event_type_matches_domain_event_class_names(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    # A rejected (overdrawing) withdrawal raises no event, so it must not
    # show up in the audit log.
    wallet_id = await command_bus.send(OpenWallet(owner_id="u-2", currency=Currency.USD))
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=9999))

    types = [e.event_type for e in audit_listener.entries_for(wallet_id)]
    assert types == ["WalletOpened", "FundsDeposited"]
    assert audit_listener.running_total(wallet_id) == 100
