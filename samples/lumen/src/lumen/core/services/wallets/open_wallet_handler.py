# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`OpenWallet`.

Creates the :class:`Wallet` aggregate, persists it, then drains its
pending events and publishes them on the EDA bus so domain-event
listeners (e.g. the wallet audit projection) can react.
"""

from __future__ import annotations

from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(self, repository: WalletRepository, events: EventPublisher) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = await self._repository.next_id()
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.add(wallet)

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
