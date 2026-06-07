# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Feature 1 — the SQLAlchemy/SQLite WalletRepository adapter (Chapter 5).

Drives the FULL wallet flow (open -> deposit -> withdraw -> get) through
the real :class:`SqlAlchemyWalletRepository` against a temporary SQLite
database file, then proves persistence by re-opening the database with a
fresh engine/session and reading the wallet back.

Everything runs with no external infrastructure: SQLite + aiosqlite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.sql_wallet_repository import (
    SqlAlchemyWalletRepository,
)
from pyfly.data.relational.sqlalchemy import Base


@pytest_asyncio.fixture
async def sqlite_session(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """A temp-file SQLite engine + session factory, schema created.

    Mirrors what PyFly's ``EngineLifecycle`` does at startup: build the
    async engine and run ``Base.metadata.create_all``. Yields the session
    factory and the database URL so the test can reconnect later.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'wallets.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, db_url
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_flow_persists_through_sqlite_adapter(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_session

    # --- open -> deposit -> withdraw through the SQLite adapter ---------
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)

        wallet_id = await repo.next_id()
        wallet = Wallet.open(wallet_id, owner_id="owner-42", currency=Currency.USD)
        await repo.add(wallet)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.deposit(Money(2500, Currency.USD))
        await repo.add(loaded)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.withdraw(Money(1000, Currency.USD))
        await repo.add(loaded)

        # get
        got = await repo.find(wallet_id)
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency is Currency.USD
        assert got.balance == Money(1500, Currency.USD)

    # --- prove persistence: reconnect with a brand-new engine/session ---
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = SqlAlchemyWalletRepository(session=fresh_session)
            persisted = await fresh_repo.find(wallet_id)
            assert persisted is not None, "wallet should survive a reconnect"
            assert persisted.balance == Money(1500, Currency.USD)
            assert persisted.owner_id == "owner-42"
            assert await fresh_repo.all_ids() == [wallet_id]
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_find_unknown_returns_none(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_session
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)
        assert await repo.find("wlt-nope") is None


@pytest.mark.asyncio
async def test_remove_deletes_the_row(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_session
    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)
        wallet = Wallet.open(await repo.next_id(), owner_id="o", currency=Currency.EUR)
        await repo.add(wallet)
        assert await repo.find(wallet.id) is not None  # type: ignore[arg-type]

        await repo.remove(wallet)
        assert await repo.find(wallet.id) is None  # type: ignore[arg-type]
        assert await repo.all_ids() == []
