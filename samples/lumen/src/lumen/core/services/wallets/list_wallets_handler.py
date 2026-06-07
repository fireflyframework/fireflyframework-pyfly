# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`ListWallets`.

Showcases framework pagination: it calls the inherited
``find_paginated(pageable=…)`` on the repository — which counts the total,
applies the ``Pageable``'s sort, and slices with ``LIMIT/OFFSET`` — then
uses :meth:`pyfly.data.Page.map` to project each :class:`WalletEntity` row
onto a :class:`WalletDto` while preserving the pagination metadata.
"""

from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListWalletsHandler(QueryHandler[ListWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_paginated(pageable=query.pageable)
        return page.map(entity_to_dto)
