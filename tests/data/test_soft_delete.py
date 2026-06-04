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
"""Tests for SoftDeleteMixin and SoftDeleteRepository."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository


class SoftOrder(BaseEntity, SoftDeleteMixin):
    __tablename__ = "soft_orders"

    name: Mapped[str] = mapped_column(String(255))


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
def repo(session):
    return SoftDeleteRepository(SoftOrder, session)


class TestSoftDeleteMixin:
    def test_deleted_at_defaults_to_none(self):
        order = SoftOrder(name="Test")
        assert order.deleted_at is None

    def test_is_deleted_false_when_not_deleted(self):
        order = SoftOrder(name="Test")
        assert order.is_deleted is False

    @pytest.mark.asyncio
    async def test_has_deleted_at_column(self, session: AsyncSession):
        order = SoftOrder(name="Test")
        session.add(order)
        await session.flush()
        assert order.deleted_at is None

    @pytest.mark.asyncio
    async def test_is_deleted_true_after_soft_delete(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="ToDelete"))
        await repo.delete(order.id)
        await repo._require_session().refresh(order)
        assert order.is_deleted is True


class TestSoftDeleteRepository:
    @pytest.mark.asyncio
    async def test_soft_delete_sets_deleted_at(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(SoftOrder(name="ToDelete"))
        await repo.delete(order.id)
        await session.refresh(order)
        assert order.deleted_at is not None

    @pytest.mark.asyncio
    async def test_find_by_id_excludes_soft_deleted(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="Deleted"))
        await repo.delete(order.id)
        found = await repo.find_by_id(order.id)
        assert found is None

    @pytest.mark.asyncio
    async def test_find_by_id_returns_non_deleted(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="Active"))
        found = await repo.find_by_id(order.id)
        assert found is not None
        assert found.name == "Active"

    @pytest.mark.asyncio
    async def test_find_all_excludes_soft_deleted(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        a = await repo.save(SoftOrder(name="Active"))
        b = await repo.save(SoftOrder(name="Deleted"))
        await repo.delete(b.id)

        items = await repo.find_all()
        assert len(items) == 1
        assert items[0].id == a.id

    @pytest.mark.asyncio
    async def test_find_all_including_deleted(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        await repo.save(SoftOrder(name="Active"))
        b = await repo.save(SoftOrder(name="Deleted"))
        await repo.delete(b.id)

        items = await repo.find_all_including_deleted()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_restore_clears_deleted_at(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="Restored"))
        await repo.delete(order.id)

        restored = await repo.restore(order.id)
        assert restored is not None
        assert restored.deleted_at is None
        assert restored.is_deleted is False

    @pytest.mark.asyncio
    async def test_restore_makes_findable(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="Restored"))
        await repo.delete(order.id)
        assert await repo.find_by_id(order.id) is None

        await repo.restore(order.id)
        found = await repo.find_by_id(order.id)
        assert found is not None
        assert found.name == "Restored"

    @pytest.mark.asyncio
    async def test_hard_delete_removes_permanently(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        order = await repo.save(SoftOrder(name="HardDelete"))
        await repo.hard_delete(order.id)

        items = await repo.find_all_including_deleted()
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_delete_all_soft_deletes_multiple(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        a = await repo.save(SoftOrder(name="A"))
        b = await repo.save(SoftOrder(name="B"))
        c = await repo.save(SoftOrder(name="C"))

        deleted_count = await repo.delete_all([a.id, b.id])
        assert deleted_count == 2

        active = await repo.find_all()
        assert len(active) == 1
        assert active[0].id == c.id

    @pytest.mark.asyncio
    async def test_delete_all_empty_list(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        deleted_count = await repo.delete_all([])
        assert deleted_count == 0

    @pytest.mark.asyncio
    async def test_count_excludes_soft_deleted(
        self,
        repo: SoftDeleteRepository[SoftOrder, UUID],
    ):
        await repo.save(SoftOrder(name="Active1"))
        await repo.save(SoftOrder(name="Active2"))
        b = await repo.save(SoftOrder(name="Deleted"))
        await repo.delete(b.id)

        assert await repo.count() == 2


class TestSoftDeletePaginatedExcludesDeleted:
    """Audit #103 — paginated / by-ids / by-spec readers must exclude soft-deleted rows."""

    @pytest.mark.asyncio
    async def test_find_paginated_excludes_deleted(self, repo, session):
        a = SoftOrder(name="a")
        b = SoftOrder(name="b")
        session.add_all([a, b])
        await session.flush()
        await repo.delete(a.id)

        page = await repo.find_paginated(page=1, size=10)
        names = {o.name for o in page.items}
        assert names == {"b"}
        assert page.total == 1  # the deleted row is not counted

    @pytest.mark.asyncio
    async def test_find_all_by_ids_excludes_deleted(self, repo, session):
        a = SoftOrder(name="a")
        b = SoftOrder(name="b")
        session.add_all([a, b])
        await session.flush()
        await repo.delete(a.id)

        found = await repo.find_all_by_ids([a.id, b.id])
        assert {o.name for o in found} == {"b"}
