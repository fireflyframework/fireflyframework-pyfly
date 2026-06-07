# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``OpenWallet`` — write-side intent for opening a wallet."""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet. Returns the generated wallet id."""

    owner_id: str
    currency: Currency

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.owner_id.strip():
            return ValidationResult.failure("owner_id", "Owner id is required")
        return ValidationResult.success()
