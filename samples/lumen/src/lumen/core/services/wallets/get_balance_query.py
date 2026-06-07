# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``GetBalance`` — read-side intent for fetching a wallet's balance."""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetBalance(Query[BalanceDto | None]):
    """Look up just the balance of a wallet by its identifier."""

    wallet_id: str
