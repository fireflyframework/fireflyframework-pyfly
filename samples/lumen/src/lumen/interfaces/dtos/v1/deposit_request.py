# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP request DTO for deposit and withdrawal endpoints.

Amounts are expressed in **minor units** (cents) to avoid floating-point
rounding errors — ``1050`` means €10.50 for an EUR wallet.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DepositRequest(BaseModel):
    """Deposit/withdrawal request payload.

    Shared by ``POST /{id}/deposit`` and ``POST /{id}/withdraw`` — both
    move a positive amount of money in the wallet's own currency.
    """

    amount: int = Field(gt=0, description="Amount in minor units (cents); must be positive")
