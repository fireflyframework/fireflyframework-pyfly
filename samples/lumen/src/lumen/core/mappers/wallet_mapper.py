# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Aggregate -> DTO mapping."""

from __future__ import annotations

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.models.entities.v1.wallet_entity import Wallet


def wallet_to_dto(wallet: Wallet) -> WalletDto:
    """Project a :class:`Wallet` aggregate onto its public :class:`WalletDto`."""
    assert wallet.id is not None
    return WalletDto(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency,
        balance_minor=wallet.balance.amount,
        balance=wallet.balance.major_units,
        created_at=wallet.created_at,
    )


def wallet_to_balance_dto(wallet: Wallet) -> BalanceDto:
    """Project a :class:`Wallet` onto the lightweight :class:`BalanceDto`."""
    assert wallet.id is not None
    return BalanceDto(
        id=wallet.id,
        currency=wallet.currency,
        balance_minor=wallet.balance.amount,
        balance=wallet.balance.major_units,
    )
