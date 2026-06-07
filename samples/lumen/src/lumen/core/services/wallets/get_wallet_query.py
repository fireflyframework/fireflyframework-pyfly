# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``GetWallet`` — read-side intent for fetching a single wallet."""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetWallet(Query[WalletDto | None]):
    """Look up a wallet by its identifier."""

    wallet_id: str
