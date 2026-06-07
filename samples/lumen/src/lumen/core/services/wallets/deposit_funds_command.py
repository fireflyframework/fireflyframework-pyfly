# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``DepositFunds`` — write-side intent for crediting a wallet."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class DepositFunds(Command[int]):
    """Deposit ``amount`` minor units. Returns the new balance (minor units)."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure("wallet_id", "Wallet id is required")
        if self.amount <= 0:
            return ValidationResult.failure("amount", "Deposit amount must be > 0")
        return ValidationResult.success()
