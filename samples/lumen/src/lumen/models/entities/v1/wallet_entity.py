# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain :class:`Wallet` aggregate.

A real DDD aggregate root built on :class:`pyfly.domain.AggregateRoot`.
State changes happen through intent-revealing methods (``open``,
``deposit``, ``withdraw``) which protect the core invariant —
**balance never goes negative** — and raise
:class:`pyfly.domain.DomainEvent` instances. The repository drains the
events with ``clear_events()`` after persisting and the application
service publishes them.

The balance is a :class:`Money` value object, so every amount carries
its currency and arithmetic stays exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent

# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletOpened(DomainEvent):
    wallet_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------


class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    __slots__ = (
        "owner_id",
        "balance",
        "created_at",
    )

    def __init__(
        self,
        id: str,
        owner_id: str,
        balance: Money,
        created_at: datetime | None = None,
    ) -> None:
        super().__init__(id)
        self.owner_id = owner_id
        self.balance = balance
        self.created_at = created_at or datetime.now(UTC)

    @property
    def currency(self) -> Currency:
        return self.balance.currency

    # --- factory ---------------------------------------------------------

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: Currency) -> Wallet:
        """Open a new, empty wallet; raises :class:`WalletOpened`."""
        if not owner_id.strip():
            raise BusinessRuleViolation("wallet-owner-required", "owner_id is required")
        wallet = cls(id=wallet_id, owner_id=owner_id, balance=Money.zero(currency))
        wallet.raise_event(WalletOpened(wallet_id=wallet_id, owner_id=owner_id, currency=currency.value))
        return wallet

    # --- transitions -----------------------------------------------------

    def deposit(self, amount: Money) -> None:
        """Credit *amount* to the balance; raises :class:`FundsDeposited`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("wallet-deposit-positive", "deposit amount must be > 0")
        self.balance = self.balance.add(amount)
        assert self.id is not None
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    def withdraw(self, amount: Money) -> None:
        """Debit *amount*; refuses to overdraw. Raises :class:`FundsWithdrawn`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("wallet-withdrawal-positive", "withdrawal amount must be > 0")
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "wallet-insufficient-funds",
                f"cannot withdraw {amount}; balance is {self.balance}",
            )
        self.balance = remaining
        assert self.id is not None
        self.raise_event(
            FundsWithdrawn(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    # --- helpers ---------------------------------------------------------

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.balance.currency:
            raise BusinessRuleViolation(
                "wallet-currency-mismatch",
                f"wallet holds {self.balance.currency.value}, got {amount.currency.value}",
            )
