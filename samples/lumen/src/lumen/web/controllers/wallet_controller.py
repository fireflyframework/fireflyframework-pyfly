# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""REST controller exposing the wallet API.

Every endpoint maps an HTTP request onto a CQRS command or query and
dispatches it through the bus — the controller holds no business logic.

The two list endpoints showcase the framework's data layer over the wire:

* ``GET /api/v1/wallets?page=&size=`` returns a *page* of wallets, built
  from the repository's ``find_paginated``;
* ``GET /api/v1/wallets/rich?min_minor=&page=&size=`` returns a page
  filtered by a composable :class:`Specification`.

Both fold a :class:`pyfly.data.Page` into the JSON-friendly
:class:`PageDto`. The collection/``rich`` routes are declared before the
``/{wallet_id}`` routes so the literal ``rich`` segment is matched ahead of
the path variable.
"""

from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.page_dto import PageDto
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.data import Pageable, Sort
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import (
    Body,
    PathVar,
    QueryParam,
    Valid,
    get_mapping,
    post_mapping,
    request_mapping,
)

#: Newest-first ordering shared by the list endpoints.
_NEWEST_FIRST = Sort.by("created_at").descending()


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, list, inspect.

    The controller injects the concrete ``DefaultCommandBus`` /
    ``DefaultQueryBus`` beans the CQRS auto-configuration registers, and
    holds no business logic — each endpoint just builds a command/query
    and dispatches it through the bus.
    """

    def __init__(self, commands: DefaultCommandBus, queries: DefaultQueryBus) -> None:
        self._commands = commands
        self._queries = queries

    # --- commands --------------------------------------------------------

    @post_mapping("", status_code=201)
    async def open_wallet(self, request: Valid[Body[OpenWalletRequest]]) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(owner_id=request.owner_id, currency=request.currency)
        )
        return {"wallet_id": wallet_id}

    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self, wallet_id: PathVar[str], request: Valid[Body[DepositRequest]]
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            DepositFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    @post_mapping("/{wallet_id}/withdraw")
    async def withdraw(
        self, wallet_id: PathVar[str], request: Valid[Body[DepositRequest]]
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            WithdrawFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # --- paged / specification queries (declare before /{wallet_id}) -----

    @get_mapping("")
    async def list_wallets(
        self, page: QueryParam[int] = 1, size: QueryParam[int] = 20
    ) -> PageDto[WalletDto]:
        """A page of wallets, newest first (``find_paginated`` + ``Page.map``)."""
        result = await self._queries.query(
            ListWallets(pageable=Pageable.of(page, size, _NEWEST_FIRST))
        )
        return PageDto.from_page(result)

    @get_mapping("/rich")
    async def list_rich_wallets(
        self,
        min_minor: QueryParam[int] = 0,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
    ) -> PageDto[WalletDto]:
        """A page of wallets with ``balance_minor >= min_minor`` (Specification)."""
        result = await self._queries.query(
            ListRichWallets(
                min_minor=min_minor,
                pageable=Pageable.of(page, size, _NEWEST_FIRST),
            )
        )
        return PageDto.from_page(result)

    # --- single-wallet queries ------------------------------------------
    #
    # The framework registers a controller's routes in alphabetical method
    # order, and Starlette matches first-registered-wins. Naming the
    # single-resource handlers ``wallet_*`` (rather than ``get_*``) sorts
    # them *after* the ``list_*`` collection handlers, so the literal
    # ``/rich`` route is matched ahead of the ``/{wallet_id}`` route.

    @get_mapping("/{wallet_id}")
    async def wallet_detail(self, wallet_id: PathVar[str]) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
