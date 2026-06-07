# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end tests for the CQRS open/deposit/withdraw + query flow."""

from __future__ import annotations

import pytest
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_full_wallet_lifecycle(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    wallet_id = await command_bus.send(OpenWallet(owner_id="u-1", currency=Currency.EUR))
    assert isinstance(wallet_id, str) and wallet_id.startswith("wlt-")

    balance = await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    assert balance == 1500

    balance = await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=500))
    assert balance == 1000

    wallet = await query_bus.query(GetWallet(wallet_id=wallet_id))
    assert wallet is not None
    assert wallet.id == wallet_id
    assert wallet.owner_id == "u-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance_minor == 1000
    assert wallet.balance == 10.0

    balance_dto = await query_bus.query(GetBalance(wallet_id=wallet_id))
    assert balance_dto is not None
    assert balance_dto.balance_minor == 1000
    assert balance_dto.balance == 10.0


@pytest.mark.asyncio
async def test_get_wallet_returns_none_for_unknown_id(query_bus: DefaultQueryBus) -> None:
    assert await query_bus.query(GetWallet(wallet_id="wlt-does-not-exist")) is None


@pytest.mark.asyncio
async def test_overdraw_is_rejected_through_the_bus(command_bus: DefaultCommandBus) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(OpenWallet(owner_id="u-2", currency=Currency.EUR))
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=100))

    with pytest.raises(CommandProcessingException):
        await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=999))


@pytest.mark.asyncio
async def test_validation_rejects_non_positive_deposit(command_bus: DefaultCommandBus) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    wallet_id = await command_bus.send(OpenWallet(owner_id="u-3", currency=Currency.EUR))
    with pytest.raises(CommandProcessingException):
        await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=0))


@pytest.mark.asyncio
async def test_deposit_to_unknown_wallet_is_rejected(command_bus: DefaultCommandBus) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(DepositFunds(wallet_id="wlt-nope", amount=100))
