# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``WalletEntity`` — the SQLAlchemy persistence row for a wallet.

This is the *on-disk* shape of a wallet: one flat row per aggregate,
mapped with SQLAlchemy 2.0 typed columns onto PyFly's declarative
:class:`~pyfly.data.relational.sqlalchemy.Base`. Inheriting ``Base``
(rather than ``BaseEntity``) lets the wallet keep its own **string**
primary key — the domain id ``wlt-…`` — instead of a surrogate UUID, so
the row and the :class:`~lumen.models.entities.v1.wallet_entity.Wallet`
aggregate share one identity.

Because the class subclasses ``Base``, importing this module registers
the ``wallets`` table in ``Base.metadata``; the framework's
``EngineLifecycle`` then creates it on startup (``ddl-auto=create``).

The framework :class:`~pyfly.data.relational.sqlalchemy.Repository`
discovers the entity type from the ``WalletRepository(Repository[WalletEntity, str])``
declaration and the primary key from the mapper, so no further wiring is
needed.

==================  ====================================================
``Wallet``          ``WalletEntity`` column
==================  ====================================================
``id``              ``id`` (string PK, e.g. ``wlt-…``)
``owner_id``        ``owner_id``
``balance.currency````currency`` (ISO-4217 code)
``balance.amount``  ``balance_minor`` (integer minor units)
``created_at``      ``created_at``
==================  ====================================================
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
