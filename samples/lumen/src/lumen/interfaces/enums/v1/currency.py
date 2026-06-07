# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Supported ISO-4217 currencies for Lumen wallets."""

from __future__ import annotations

from enum import StrEnum


class Currency(StrEnum):
    """ISO-4217 currency codes Lumen wallets can hold.

    Kept small on purpose — a wallet holds exactly one currency for its
    whole lifetime, and deposits/withdrawals must match it.
    """

    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"
