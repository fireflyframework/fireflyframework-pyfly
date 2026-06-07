# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`GetBalance`.

Loads the wallet row and projects it onto the lightweight
:class:`BalanceDto` through the framework :class:`~pyfly.data.Mapper` and a
``@projection`` interface — the read side only ever touches the columns the
balance view declares.
"""

from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
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
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
