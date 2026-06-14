# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``ListRichWallets`` — a page of wallets at or above a balance threshold.

Carries the threshold (``min_minor``) and a :class:`pyfly.data.Pageable`.
The handler runs the repository's Specification-backed ``find_rich``,
which composes a reusable predicate and paginates it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.cqrs import Query
from pyfly.data import Page, Pageable


@dataclass(frozen=True)
class ListRichWallets(Query[Page[WalletDto]]):
    """List wallets whose balance is at least ``min_minor``, paged."""

    min_minor: int
    pageable: Pageable
