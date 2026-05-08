# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``PlaceOrderCommand`` — write-side intent for the place-order flow."""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class PlaceOrderCommand(Command[str]):
    """Place a new order. Returns the generated order id."""

    sku: str
    quantity: int
    unit_price: float

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.sku.strip():
            return ValidationResult.failure("sku", "SKU is required")
        if self.quantity <= 0:
            return ValidationResult.failure("quantity", "Quantity must be > 0")
        if self.unit_price <= 0:
            return ValidationResult.failure("unit_price", "Unit price must be > 0")
        return ValidationResult.success()
