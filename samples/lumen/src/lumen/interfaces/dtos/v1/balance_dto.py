# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Balance read-side types for ``GET /api/v1/wallets/{id}/balance``.

Two shapes live here:

* :class:`BalanceView` — a ``@projection``-marked class naming the subset
  of entity fields the balance view needs (plus a computed ``balance``).
  It is the Spring-Data projection: the :class:`~pyfly.data.Mapper` reads
  exactly these fields off the entity (and applies the registered
  ``balance`` transform) and constructs this type.
* :class:`BalanceDto` — the concrete Pydantic response model the HTTP
  layer serialises.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.data import projection


@projection
@dataclass
class BalanceView:
    """Projection: just the fields the balance view needs.

    ``id``, ``currency`` and ``balance_minor`` are copied straight from
    the :class:`~lumen.models.entities.v1.wallet_orm.WalletEntity`;
    ``balance`` is a computed major-unit decimal supplied by a registered
    transform on the mapper. Marked ``@projection`` to declare intent and
    constructed by :meth:`pyfly.data.Mapper.project`.
    """

    id: str
    currency: str
    balance_minor: int
    balance: float


class BalanceDto(BaseModel):
    """Lightweight balance projection for the balance endpoint."""

    id: str
    currency: Currency
    balance_minor: int
    balance: float
