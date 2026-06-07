# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the :class:`Wallet` aggregate root."""

from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import (
    FundsDeposited,
    FundsWithdrawn,
    Wallet,
    WalletOpened,
)

from pyfly.domain import BusinessRuleViolation


def test_open_creates_empty_wallet() -> None:
    wallet = Wallet.open("wlt-1", "owner-1", Currency.EUR)
    assert wallet.owner_id == "owner-1"
    assert wallet.currency is Currency.EUR
    assert wallet.balance == Money.zero(Currency.EUR)
    [event] = wallet.pending_events()
    assert isinstance(event, WalletOpened)
    assert event.wallet_id == "wlt-1"
    assert event.currency == "EUR"


def test_open_requires_owner() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Wallet.open("wlt-x", "   ", Currency.EUR)
    assert exc.value.rule == "wallet-owner-required"


def test_deposit_then_withdraw_happy_path() -> None:
    wallet = Wallet.open("wlt-2", "owner-2", Currency.EUR)
    wallet.clear_events()

    wallet.deposit(Money(1000, Currency.EUR))
    assert wallet.balance == Money(1000, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsDeposited)
    assert event.amount == 1000
    assert event.balance == 1000

    wallet.withdraw(Money(400, Currency.EUR))
    assert wallet.balance == Money(600, Currency.EUR)
    [event] = wallet.clear_events()
    assert isinstance(event, FundsWithdrawn)
    assert event.amount == 400
    assert event.balance == 600


def test_withdraw_cannot_overdraw() -> None:
    wallet = Wallet.open("wlt-3", "owner-3", Currency.EUR)
    wallet.deposit(Money(500, Currency.EUR))
    wallet.clear_events()
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.withdraw(Money(501, Currency.EUR))
    assert exc.value.rule == "wallet-insufficient-funds"
    # invariant held: balance unchanged, no event raised
    assert wallet.balance == Money(500, Currency.EUR)
    assert wallet.pending_events() == []


def test_deposit_must_be_positive() -> None:
    wallet = Wallet.open("wlt-4", "owner-4", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(0, Currency.EUR))
    assert exc.value.rule == "wallet-deposit-positive"


def test_currency_mismatch_is_rejected() -> None:
    wallet = Wallet.open("wlt-5", "owner-5", Currency.EUR)
    with pytest.raises(BusinessRuleViolation) as exc:
        wallet.deposit(Money(100, Currency.USD))
    assert exc.value.rule == "wallet-currency-mismatch"
