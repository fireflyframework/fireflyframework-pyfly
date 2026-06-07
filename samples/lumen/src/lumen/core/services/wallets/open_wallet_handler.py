# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`OpenWallet`.

Generates the wallet id, creates the :class:`Wallet` aggregate, persists
it through the framework :class:`WalletRepository`, then drains the
aggregate's pending events and publishes them on the EDA bus so listeners
(e.g. the wallet audit projection) can react.

The handler runs inside ``@transactional()``: the decorator opens a unit
of work from the injected ``async_sessionmaker`` (``self._session_factory``),
swaps that session onto the repository for the call, commits on success,
and rolls back on failure. Without it the framework's single shared
session would only *flush* — never commit — so the write would not survive.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        # @transactional resolves the unit-of-work session from here.
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
