# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``WithdrawFunds`` — write-side intent for debiting a wallet."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class WithdrawFunds(Command[int]):
    """Withdraw ``amount`` minor units. Returns the new balance (minor units)."""

    wallet_id: str
    amount: int

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.wallet_id.strip():
            return ValidationResult.failure("wallet_id", "Wallet id is required")
        if self.amount <= 0:
            return ValidationResult.failure("amount", "Withdrawal amount must be > 0")
        return ValidationResult.success()
