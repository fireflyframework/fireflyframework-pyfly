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
"""Tests for VersionedMixin and optimistic locking."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, VersionedMixin
from pyfly.data.relational.sqlalchemy.repository import Repository


class VersionedOrder(BaseEntity, VersionedMixin):
    __tablename__ = "versioned_orders"

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
    return Repository(VersionedOrder, session)


class TestVersionedMixin:
    @pytest.mark.asyncio
    async def test_version_starts_at_one_after_insert(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="New"))
        assert order.version == 1

    @pytest.mark.asyncio
    async def test_version_increments_on_update(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="Original"))
        assert order.version == 1

        order.name = "Updated"
        await session.flush()
        await session.refresh(order)
        assert order.version == 2

    @pytest.mark.asyncio
    async def test_version_increments_again(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="V1"))
        assert order.version == 1

        order.name = "V2"
        await session.flush()
        await session.refresh(order)
        assert order.version == 2

        order.name = "V3"
        await session.flush()
        await session.refresh(order)
        assert order.version == 3

    @pytest.mark.asyncio
    async def test_stale_version_raises_error(self, session_factory):
        async with session_factory() as s1:
            order = VersionedOrder(name="Shared")
            s1.add(order)
            await s1.commit()
            order_id = order.id

        async with session_factory() as s1:
            result1 = await s1.get(VersionedOrder, order_id)
            assert result1 is not None
            result1.name = "Updated by S1"

            async with session_factory() as s2:
                result2 = await s2.get(VersionedOrder, order_id)
                assert result2 is not None
                result2.name = "Updated by S2"
                await s2.commit()

            with pytest.raises(StaleDataError):
                await s1.commit()

    @pytest.mark.asyncio
    async def test_version_column_exists(self, session: AsyncSession):
        order = VersionedOrder(name="Test")
        session.add(order)
        await session.flush()
        assert hasattr(order, "version")
        assert isinstance(order.version, int)
