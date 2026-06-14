# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``Money`` — an immutable DDD value object.

Money is the textbook value object: it has no identity, two instances
with the same amount and currency are interchangeable, and it never
mutates. Lumen stores amounts as **integer minor units** (cents) plus an
ISO-4217 currency code so arithmetic is exact — no floating-point drift.

Built on :class:`pyfly.domain.ValueObject`, decorated with
``@dataclass(frozen=True)`` exactly as the base class requires.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.domain import BusinessRuleViolation, ValueObject


@dataclass(frozen=True)
class Money(ValueObject):
    """An exact monetary amount in a single currency.

    ``amount`` is in minor units (e.g. cents): ``Money(1050, Currency.EUR)``
    is €10.50. Arithmetic returns new ``Money`` instances and refuses to
    mix currencies.
    """

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise BusinessRuleViolation("money-amount-integer", "amount must be an integer number of minor units")

    # --- factories -------------------------------------------------------

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """The additive identity for *currency* (a zero balance)."""
        return cls(amount=0, currency=currency)

    # --- arithmetic ------------------------------------------------------

    def add(self, other: Money) -> Money:
        """Return ``self + other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: Money) -> Money:
        """Return ``self - other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    # --- predicates ------------------------------------------------------

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    @property
    def major_units(self) -> float:
        """The amount rendered as a major-unit decimal (cents / 100)."""
        return round(self.amount / 100, 2)

    # --- helpers ---------------------------------------------------------

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise BusinessRuleViolation(
                "money-currency-mismatch",
                f"cannot combine {self.currency.value} with {other.currency.value}",
            )

    def __str__(self) -> str:
        return f"{self.major_units:.2f} {self.currency.value}"
