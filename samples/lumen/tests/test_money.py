# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the :class:`Money` value object."""

from __future__ import annotations

import pytest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money

from pyfly.domain import BusinessRuleViolation


def test_value_equality_is_structural() -> None:
    assert Money(1050, Currency.EUR) == Money(1050, Currency.EUR)
    assert Money(1050, Currency.EUR) != Money(1050, Currency.USD)
    assert Money(1050, Currency.EUR) != Money(999, Currency.EUR)


def test_money_is_immutable() -> None:
    money = Money(1050, Currency.EUR)
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        money.amount = 0  # type: ignore[misc]


def test_add_and_subtract_same_currency() -> None:
    a = Money(1050, Currency.EUR)
    b = Money(450, Currency.EUR)
    assert a.add(b) == Money(1500, Currency.EUR)
    assert a.subtract(b) == Money(600, Currency.EUR)


def test_zero_factory_and_major_units() -> None:
    assert Money.zero(Currency.USD) == Money(0, Currency.USD)
    assert Money(1050, Currency.EUR).major_units == 10.5
    assert str(Money(1050, Currency.EUR)) == "10.50 EUR"


def test_currency_mismatch_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(100, Currency.EUR).add(Money(100, Currency.USD))
    assert exc.value.rule == "money-currency-mismatch"


def test_non_integer_amount_is_rejected() -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Money(10.5, Currency.EUR)  # type: ignore[arg-type]
    assert exc.value.rule == "money-amount-integer"
