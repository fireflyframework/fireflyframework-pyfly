# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Integration tests: SQLAlchemy Repository + SoftDeleteRepository against real Postgres (asyncpg).

These tests exercise dialect-specific behaviour that aiosqlite cannot faithfully replicate:
  - UUID primary key round-trips through the Postgres ``uuid`` type
  - ``ILIKE`` case-insensitive pattern matching (Postgres extension)
  - ``TIMESTAMPTZ`` timezone-aware columns stored and retrieved with tz info
  - Pagination with real Postgres query planner
  - Specification composition (``&`` / ``|``) on real data
  - Soft-delete life-cycle (hidden from reads → restore → hard-delete)

Run only when Docker is available (the ``pg_url`` fixture starts a testcontainer).
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.testing import requires_docker

# ---------------------------------------------------------------------------
# Domain models — unique table names to avoid conflicts across parallel runs
# ---------------------------------------------------------------------------

_SUFFIX = uuid.uuid4().hex[:8]


class PgProduct(BaseEntity):
    """Product entity for Postgres dialect tests."""

    __tablename__ = f"pg_products_{_SUFFIX}"

    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[float] = mapped_column(default=0.0)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)


class PgSoftItem(BaseEntity, SoftDeleteMixin):
    """Soft-delete entity for Postgres dialect tests."""

    __tablename__ = f"pg_soft_items_{_SUFFIX}"

    label: Mapped[str] = mapped_column(String(200))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ilike_spec(pattern: str) -> Specification[PgProduct]:
    """Postgres ILIKE (case-insensitive LIKE) — not available on aiosqlite."""
    return Specification(lambda root, q: q.where(root.name.ilike(pattern)))


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_postgres_repository_full(pg_url: str) -> None:
    """Full Repository smoke-test on real Postgres: UUID PKs, pagination, spec, soft-delete."""
    engine = create_async_engine(pg_url, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        # --- 1. UUID primary-key round-trip -----------------------------------
        async with session_factory() as session:
            repo: Repository[PgProduct, UUID] = Repository(PgProduct, session)
            product = PgProduct(name="Gadget", price=49.99, category="electronics")
            saved = await repo.save(product)

        assert isinstance(saved.id, UUID), "Postgres must echo back a proper UUID"
        saved_id = saved.id

        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            found = await repo.find_by_id(saved_id)
        assert found is not None
        assert found.name == "Gadget"
        assert found.price == 49.99

        # --- 2. Timestamptz: created_at retains timezone info -----------------
        assert found.created_at.tzinfo is not None, "Postgres TIMESTAMPTZ must preserve timezone"

        # --- 3. find_all with **filters ---------------------------------------
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            await repo.save(PgProduct(name="Thingamajig", price=9.99, category="misc"))
            await repo.save(PgProduct(name="Doohickey", price=19.99, category="misc"))

        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            misc = await repo.find_all(category="misc")
        assert len(misc) == 2

        # --- 4. find_paginated (page / size / total) --------------------------
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            for i in range(10):
                await repo.save(PgProduct(name=f"Item-{i:02d}", price=float(i), category="bulk"))

        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            page: Page[PgProduct] = await repo.find_paginated(page=1, size=4)
        assert isinstance(page, Page)
        assert len(page.items) <= 4
        assert page.total >= 10  # other items saved above also counted

        # Sorted pagination
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            pageable = Pageable.of(1, 5, Sort(orders=(Order.desc("price"),)))
            page2: Page[PgProduct] = await repo.find_paginated(pageable=pageable)
        assert len(page2.items) <= 5
        assert page2.total >= 10

        # --- 5. Specification query -------------------------------------------
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            expensive = Specification(lambda root, q: q.where(root.price >= 5.0))
            results = await repo.find_all_by_spec(expensive)
        assert any(p.price >= 5.0 for p in results)

        # --- 6. Postgres-specific ILIKE (case-insensitive match) --------------
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            await repo.save(PgProduct(name="SuperWidget", price=99.0, category="special"))
            await repo.save(PgProduct(name="superwidget_v2", price=89.0, category="special"))

        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            ilike_results = await repo.find_all_by_spec(_ilike_spec("superwidget%"))
        assert len(ilike_results) == 2, "ILIKE must match both case variants"

        # --- 7. Specification AND composition ---------------------------------
        async with session_factory() as session:
            repo = Repository(PgProduct, session)
            in_special = Specification(lambda root, q: q.where(root.category == "special"))
            pricey = Specification(lambda root, q: q.where(root.price >= 95.0))
            combined = await repo.find_all_by_spec(in_special & pricey)
        assert all(p.category == "special" and p.price >= 95.0 for p in combined)
        assert len(combined) >= 1

        # --- 8. Soft-delete lifecycle -----------------------------------------
        async with session_factory() as session:
            sd_repo: SoftDeleteRepository[PgSoftItem, UUID] = SoftDeleteRepository(PgSoftItem, session)
            item_a = await sd_repo.save(PgSoftItem(label="alpha"))
            item_b = await sd_repo.save(PgSoftItem(label="beta"))

        soft_id = item_a.id

        # soft-delete item_a
        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            await sd_repo.delete(soft_id)

        # find_by_id must hide deleted row
        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            assert await sd_repo.find_by_id(soft_id) is None, "Soft-deleted row must be invisible"
            visible = await sd_repo.find_all()
        assert all(i.label != "alpha" for i in visible), "find_all must exclude soft-deleted rows"

        # restore
        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            restored = await sd_repo.restore(soft_id)
        assert restored is not None
        assert restored.deleted_at is None

        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            assert await sd_repo.find_by_id(soft_id) is not None, "Restored row must be visible again"

        # hard-delete
        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            await sd_repo.hard_delete(soft_id)
            assert await sd_repo.find_by_id(soft_id) is None

        # --- 9. count / exists ------------------------------------------------
        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            count = await sd_repo.count()
        # item_b still present (item_a hard-deleted)
        assert count >= 1

        async with session_factory() as session:
            sd_repo = SoftDeleteRepository(PgSoftItem, session)
            assert await sd_repo.exists(item_b.id) is True
            assert await sd_repo.exists(uuid.uuid4()) is False

    finally:
        async with engine.begin() as conn:
            # Drop only the tables we created so we don't disturb other fixtures
            for tbl in reversed(Base.metadata.sorted_tables):
                if tbl.name in (PgProduct.__tablename__, PgSoftItem.__tablename__):
                    await conn.run_sync(lambda c, t=tbl: t.drop(c, checkfirst=True))  # noqa: B023
        await engine.dispose()
