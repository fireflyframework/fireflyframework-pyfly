# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`DepositFunds`.

Loads the wallet, applies the deposit through the aggregate (which
enforces the currency and protects the balance invariant), persists it,
and drains the pending events.
"""

from __future__ import annotations

import logging

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound

logger = logging.getLogger(__name__)


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, command: DepositFunds) -> int:  # type: ignore[override]
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.add(wallet)

        for event in wallet.clear_events():
            logger.info(
                "domain_event",
                extra={"event_type": event.event_type, "event_id": event.event_id},
            )
        return wallet.balance.amount
