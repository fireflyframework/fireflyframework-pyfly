# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP response DTO for ``GET /api/v1/wallets/{id}/balance``."""

from __future__ import annotations

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class BalanceDto(BaseModel):
    """Lightweight balance projection for the balance endpoint."""

    id: str
    currency: Currency
    balance_minor: int
    balance: float
