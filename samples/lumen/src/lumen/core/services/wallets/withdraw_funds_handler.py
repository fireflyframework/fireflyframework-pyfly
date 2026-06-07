# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`WithdrawFunds`.

Loads the wallet and applies the withdrawal through the aggregate, which
refuses to overdraw (``balance >= 0``). Persists, then drains the pending
events and publishes them on the EDA bus.
"""

from __future__ import annotations

from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class WithdrawFundsHandler(CommandHandler[WithdrawFunds, int]):
    """Debit funds from an existing wallet; returns the new balance."""

    def __init__(self, repository: WalletRepository, events: EventPublisher) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: WithdrawFunds) -> int:  # type: ignore[override]
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.withdraw(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.add(wallet)

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
