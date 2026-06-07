# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""REST controller exposing the wallet API.

Every endpoint maps an HTTP request onto a CQRS command or query and
dispatches it through the bus — the controller holds no business logic.
"""

from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import Body, PathVar, Valid, get_mapping, post_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, inspect.

    The controller injects the concrete ``DefaultCommandBus`` /
    ``DefaultQueryBus`` beans the CQRS auto-configuration registers, and
    holds no business logic — each endpoint just builds a command/query
    and dispatches it through the bus.
    """

    def __init__(self, commands: DefaultCommandBus, queries: DefaultQueryBus) -> None:
        self._commands = commands
        self._queries = queries

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

    @get_mapping("/{wallet_id}")
    async def get_wallet(self, wallet_id: PathVar[str]) -> WalletDto:
        result = await self._queries.query(GetWallet(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    @get_mapping("/{wallet_id}/balance")
    async def get_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
