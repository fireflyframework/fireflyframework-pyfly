# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Feature 1 — the framework :class:`WalletRepository` over SQLite (Chapter 5).

Exercises the Spring-Data-style repository the application boots on: the
inherited CRUD surface (``save``/``upsert``, ``find_by_id``, ``count``,
``find_all(pageable)``), the **derived query** (``find_by_owner_id``, compiled
from the method name by the real ``RepositoryBeanPostProcessor``), and the
**Specification** path (``find_rich`` / ``find_all_by_spec``). It then
proves persistence by re-opening the database with a fresh engine/session.

Everything runs with no external infrastructure: SQLite + aiosqlite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories.wallet_repository import (
    WalletRepository,
    balance_at_least,
)
from pyfly.data import Pageable, Sort
from pyfly.data.relational.sqlalchemy import Base
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor


def _entity(wid: str, owner: str, minor: int, *, currency: str = "EUR", age_days: int = 0) -> WalletEntity:
    created = datetime.now(UTC) - timedelta(days=age_days)
    return WalletEntity(id=wid, owner_id=owner, currency=currency, balance_minor=minor, created_at=created)


def _make_repo(session: AsyncSession) -> WalletRepository:
    repo = WalletRepository(WalletEntity, session)
    # Mirror the ApplicationContext: compile derived-query stubs onto the bean.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    return repo


@pytest_asyncio.fixture
async def sqlite_factory(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
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
async def test_upsert_inserts_then_updates_and_persists(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_factory

    # --- INSERT then UPDATE through upsert, committing the unit of work --
    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "owner-42", 0, currency="USD"))
        # update: same PK, new balance
        await repo.upsert(_entity("wlt-1", "owner-42", 2500, currency="USD"))
        await session.commit()

        got = await repo.find_by_id("wlt-1")
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency == "USD"
        assert got.balance_minor == 2500
        assert await repo.count() == 1

    # --- prove persistence: reconnect with a brand-new engine/session ---
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = _make_repo(fresh_session)
            persisted = await fresh_repo.find_by_id("wlt-1")
            assert persisted is not None, "wallet should survive a reconnect"
            assert persisted.balance_minor == 2500
    finally:
        await fresh_engine.dispose()


@pytest.mark.asyncio
async def test_find_by_id_unknown_returns_none(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        assert await repo.find_by_id("wlt-nope") is None


@pytest.mark.asyncio
async def test_derived_find_by_owner_id(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "alice", 100))
        await repo.upsert(_entity("wlt-2", "alice", 200))
        await repo.upsert(_entity("wlt-3", "bob", 300))
        await session.commit()

        owned = await repo.find_by_owner_id("alice")
        assert sorted(w.id for w in owned) == ["wlt-1", "wlt-2"]
        assert await repo.find_by_owner_id("nobody") == []


@pytest.mark.asyncio
async def test_specification_find_rich_paged_and_sorted(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        # age_days drives created_at so we can assert newest-first ordering.
        await repo.upsert(_entity("wlt-poor", "a", 50, age_days=3))
        await repo.upsert(_entity("wlt-mid", "b", 1000, age_days=2))
        await repo.upsert(_entity("wlt-rich", "c", 5000, age_days=1))
        await session.commit()

        # Specification: balance_minor >= 1000, newest first, page size 1.
        newest_first = Sort.by("created_at").descending()
        page = await repo.find_rich(1000, Pageable.of(1, 1, newest_first))
        assert page.total == 2  # mid + rich
        assert page.total_pages == 2
        assert page.has_next is True
        assert [w.id for w in page.items] == ["wlt-rich"]  # newest of the two

        page2 = await repo.find_rich(1000, Pageable.of(2, 1, newest_first))
        assert [w.id for w in page2.items] == ["wlt-mid"]

        # The bare predicate also works through find_all_by_spec.
        rich = await repo.find_all_by_spec(balance_at_least(5000))
        assert [w.id for w in rich] == ["wlt-rich"]


@pytest.mark.asyncio
async def test_find_all_pageable_counts_and_pages(
    sqlite_factory: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        for i in range(5):
            await repo.upsert(_entity(f"wlt-{i}", "owner", i * 100, age_days=5 - i))
        await session.commit()

        page = await repo.find_all(Pageable.of(1, 2, Sort.by("created_at").descending()))
        assert page.total == 5
        assert page.total_pages == 3
        assert len(page.items) == 2
        # newest first -> wlt-4 (age 1 day) then wlt-3
        assert [w.id for w in page.items] == ["wlt-4", "wlt-3"]
