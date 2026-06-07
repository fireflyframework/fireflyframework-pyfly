# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`ListRichWallets`.

Showcases the framework :class:`~pyfly.data.relational.sqlalchemy.Specification`
path: it calls the repository's ``find_rich(min_minor, pageable)``, which
builds a composable ``balance_minor >= min_minor`` predicate and runs it
through ``find_all_by_spec_paged`` (WHERE + sort + LIMIT/OFFSET + count).
The resulting :class:`~pyfly.data.Page` of rows is mapped to
:class:`WalletDto`.
"""

from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler
from pyfly.data import Page


@query_handler
@service
class ListRichWalletsHandler(QueryHandler[ListRichWallets, Page[WalletDto]]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: ListRichWallets) -> Page[WalletDto]:  # type: ignore[override]
        page = await self._repository.find_rich(query.min_minor, query.pageable)
        return page.map(entity_to_dto)
