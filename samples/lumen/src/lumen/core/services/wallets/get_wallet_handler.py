# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`GetWallet`.

A read-side handler: it loads the wallet row through ``find_by_id`` and
projects it onto the public :class:`WalletDto`. Reads do not mutate, so no
transaction is needed — the repository's injected session is used directly.
"""

from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_dto
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetWalletHandler(QueryHandler[GetWallet, WalletDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetWallet) -> WalletDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_dto(entity) if entity is not None else None
