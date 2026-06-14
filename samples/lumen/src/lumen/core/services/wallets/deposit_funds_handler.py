# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`DepositFunds`.

Loads the wallet row, rehydrates the :class:`Wallet` aggregate, applies
the deposit through it (the aggregate enforces the currency and protects
the ``balance >= 0`` invariant), persists the new state, then drains and
publishes the pending events.

Runs inside ``@transactional()`` so the load-mutate-save sequence is one
committed unit of work over a single session.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: DepositFunds) -> int:  # type: ignore[override]
        entity = await self._repository.find_by_id(command.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet = to_aggregate(entity)
        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
