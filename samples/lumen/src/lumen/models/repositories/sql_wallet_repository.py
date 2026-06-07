# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""SQLAlchemy/SQLite adapter for the :class:`WalletRepository` port.

This is a *second* adapter for the same hexagonal port the core depends
on (the first is :class:`InMemoryWalletRepository`). The business logic —
the :class:`Wallet` aggregate and the CQRS handlers — never changes; only
the adapter behind the port does.

The adapter persists wallets to a real relational database through
PyFly's SQLAlchemy data layer (``pyfly[data-relational]``). With the
default ``sqlite+aiosqlite`` URL it runs with **no external infra** — the
schema is created from :data:`Base.metadata` on application startup by the
framework's ``EngineLifecycle`` bean.

Mapping
-------
The :class:`Wallet` aggregate (id, owner_id, :class:`Money` balance,
created_at) is mapped onto a flat :class:`WalletRow`:

==================  ====================================================
``Wallet``          ``WalletRow``
==================  ====================================================
``id``              ``id`` (string PK, e.g. ``wlt-…``)
``owner_id``        ``owner_id``
``balance.currency````currency`` (ISO-4217 code)
``balance.amount``  ``balance_minor`` (integer minor units)
``created_at``      ``created_at``
==================  ====================================================
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Base

# ---------------------------------------------------------------------------
# Persistence row (the SQLAlchemy mapping)
# ---------------------------------------------------------------------------


class WalletRow(Base):
    """The on-disk shape of a wallet — one row per aggregate.

    Inherits PyFly's :class:`Base` declarative base, so the table is part
    of ``Base.metadata`` and the framework creates it on startup
    (``ddl-auto=create``). The primary key is the aggregate's own string
    id (``wlt-…``) rather than a surrogate, keeping the row and the
    aggregate in lock-step.
    """

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


@repository
class SqlAlchemyWalletRepository(WalletRepository):
    """Relational adapter backed by SQLAlchemy 2.0 + SQLite (async).

    Explicitly implements the :class:`WalletRepository` port so the DI
    container binds the port to it. It is **not** marked ``@primary`` —
    :class:`InMemoryWalletRepository` keeps that role — so the app boots
    on the in-memory store while this adapter remains selectable (resolve
    it by name/type, or make it primary, to run on SQLite).

    The :class:`AsyncSession` is injected by the framework's relational
    auto-configuration (``pyfly.data.relational.enabled=true``).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- port methods ----------------------------------------------------

    async def add(self, wallet: Wallet) -> Wallet:
        """Upsert the aggregate, then commit the unit of work."""
        assert wallet.id is not None
        row = await self._session.get(WalletRow, wallet.id)
        if row is None:
            row = WalletRow(
                id=wallet.id,
                owner_id=wallet.owner_id,
                currency=wallet.currency.value,
                balance_minor=wallet.balance.amount,
                created_at=wallet.created_at,
            )
            self._session.add(row)
        else:
            row.owner_id = wallet.owner_id
            row.currency = wallet.currency.value
            row.balance_minor = wallet.balance.amount
        await self._session.commit()
        return wallet

    async def find(self, id: str) -> Wallet | None:
        """Load a wallet by id, rehydrating the aggregate from its row."""
        row = await self._session.get(WalletRow, id)
        return self._to_aggregate(row) if row is not None else None

    async def remove(self, wallet: Wallet) -> None:
        """Delete the wallet's row, then commit."""
        if wallet.id is None:
            return
        row = await self._session.get(WalletRow, wallet.id)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()

    async def next_id(self) -> str:
        return f"wlt-{uuid.uuid4()}"

    # --- mapping ---------------------------------------------------------

    @staticmethod
    def _to_aggregate(row: WalletRow) -> Wallet:
        """Rehydrate a :class:`Wallet` aggregate from a persistence row."""
        currency = Currency(row.currency)
        return Wallet(
            id=row.id,
            owner_id=row.owner_id,
            balance=Money(amount=row.balance_minor, currency=currency),
            created_at=row.created_at,
        )

    # Exposed for tests / ad-hoc queries: list every persisted wallet id.
    async def all_ids(self) -> list[str]:
        result = await self._session.execute(select(WalletRow.id))
        return list(result.scalars().all())
