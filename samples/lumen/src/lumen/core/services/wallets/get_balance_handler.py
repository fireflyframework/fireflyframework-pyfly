# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`GetBalance`."""

from __future__ import annotations

from lumen.core.mappers.wallet_mapper import wallet_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetBalance) -> BalanceDto | None:  # type: ignore[override]
        wallet = await self._repository.find(query.wallet_id)
        return wallet_to_balance_dto(wallet) if wallet is not None else None
