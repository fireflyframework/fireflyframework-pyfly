# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``ListWallets`` — read-side intent for a page of wallets.

Carries a :class:`pyfly.data.Pageable` (page number, size, sort). The
handler runs it through the repository's ``find_paginated`` and returns a
:class:`~pyfly.data.Page` of :class:`WalletDto`.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.data import Page, Pageable
from pyfly.cqrs import Query


@dataclass(frozen=True)
class ListWallets(Query[Page[WalletDto]]):
    """List wallets, one page at a time."""

    pageable: Pageable
