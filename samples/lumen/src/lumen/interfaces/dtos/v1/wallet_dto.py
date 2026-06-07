# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP response DTO for ``GET /api/v1/wallets/{id}``."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class WalletDto(BaseModel):
    """Full wallet representation returned to clients.

    ``balance_minor`` is in minor units (cents); ``balance`` is the same
    value rendered as a major-unit decimal for human-friendly display.
    """

    id: str
    owner_id: str
    currency: Currency
    balance_minor: int
    balance: float
    created_at: datetime
