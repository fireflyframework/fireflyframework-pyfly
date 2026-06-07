# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`OpenWallet`.

Creates the :class:`Wallet` aggregate, persists it, and drains its
pending events so the application can publish them to the event bus.
"""

from __future__ import annotations

import logging

from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler

logger = logging.getLogger(__name__)


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = await self._repository.next_id()
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.add(wallet)

        for event in wallet.clear_events():
            logger.info(
                "domain_event",
                extra={"event_type": event.event_type, "event_id": event.event_id},
            )
        return wallet_id
