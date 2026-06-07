# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Mapping between the wallet aggregate, its persistence row, and DTOs.

Three directions live here, each a small pure function:

* ``to_entity`` / ``to_aggregate`` translate between the rich domain
  :class:`~lumen.models.entities.v1.wallet_entity.Wallet` aggregate and the
  flat :class:`~lumen.models.entities.v1.wallet_orm.WalletEntity` row the
  framework :class:`~pyfly.data.relational.sqlalchemy.Repository` stores.
* ``entity_to_dto`` / ``entity_to_balance_dto`` project a persisted row
  onto the public read DTOs returned by the query side.

The balance projection is built with the framework's reflective
:class:`~pyfly.data.Mapper` against a ``@projection``-marked interface
(:class:`~lumen.interfaces.dtos.v1.balance_dto.BalanceView`) — the
Spring-Data "interface projection" idea: declare the subset of fields you
want and let the mapper copy exactly those.
"""

from __future__ import annotations

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto, BalanceView
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.data import Mapper

# A reusable reflective mapper. ``register_projection`` declares the
# computed ``balance`` field (major units) on top of the columns the
# BalanceView projection copies straight from the entity.
_mapper = Mapper()
_mapper.register_projection(
    WalletEntity,
    BalanceView,
    transforms={"balance": lambda e: round(e.balance_minor / 100, 2)},
)


# ---------------------------------------------------------------------------
# Aggregate  <->  persistence row
# ---------------------------------------------------------------------------


def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a :class:`Wallet` aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )


def to_aggregate(entity: WalletEntity) -> Wallet:
    """Rehydrate a :class:`Wallet` aggregate from a persistence row."""
    currency = Currency(entity.currency)
    return Wallet(
        id=entity.id,
        owner_id=entity.owner_id,
        balance=Money(amount=entity.balance_minor, currency=currency),
        created_at=entity.created_at,
    )


# ---------------------------------------------------------------------------
# Persistence row  ->  read DTOs
# ---------------------------------------------------------------------------


def entity_to_dto(entity: WalletEntity) -> WalletDto:
    """Project a persisted row onto the public :class:`WalletDto`."""
    return WalletDto(
        id=entity.id,
        owner_id=entity.owner_id,
        currency=Currency(entity.currency),
        balance_minor=entity.balance_minor,
        balance=round(entity.balance_minor / 100, 2),
        created_at=entity.created_at,
    )


def entity_to_balance_dto(entity: WalletEntity) -> BalanceDto:
    """Project a row onto the lightweight balance DTO via a projection.

    Goes through :meth:`pyfly.data.Mapper.project` against the
    ``@projection`` interface :class:`BalanceView` — only the declared
    fields are read, and the registered transform computes ``balance``.
    """
    view = _mapper.project(entity, BalanceView)
    return BalanceDto(
        id=view.id,
        currency=Currency(view.currency),
        balance_minor=view.balance_minor,
        balance=view.balance,
    )
