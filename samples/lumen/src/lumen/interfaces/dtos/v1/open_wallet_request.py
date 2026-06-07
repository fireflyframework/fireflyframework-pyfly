# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP request DTO for ``POST /api/v1/wallets``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from lumen.interfaces.enums.v1.currency import Currency


class OpenWalletRequest(BaseModel):
    """Wallet-opening request payload."""

    owner_id: str = Field(min_length=1, max_length=64, description="Identifier of the wallet owner")
    currency: Currency = Field(default=Currency.EUR, description="ISO-4217 currency the wallet holds")
