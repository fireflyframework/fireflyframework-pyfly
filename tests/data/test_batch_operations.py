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
"""Tests for batch operations on the SQLAlchemy Repository."""

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.repository import Repository


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def repo(session):
    return Repository[Item, int](Item, session)


class TestBatchOperations:
    @pytest.mark.asyncio
    async def test_save_all(self, repo, session):
        items = [Item(name="a"), Item(name="b"), Item(name="c")]
        result = await repo.save_all(items)
        assert len(result) == 3
        assert all(item.id is not None for item in result)

    @pytest.mark.asyncio
    async def test_find_all_by_ids(self, repo, session):
        items = [Item(name="x"), Item(name="y"), Item(name="z")]
        saved = await repo.save_all(items)
        ids = [item.id for item in saved]

        found = await repo.find_all_by_ids(ids[:2])
        assert len(found) == 2
        assert {item.name for item in found} == {"x", "y"}

    @pytest.mark.asyncio
    async def test_find_all_by_ids_empty(self, repo):
        found = await repo.find_all_by_ids([])
        assert found == []

    @pytest.mark.asyncio
    async def test_delete_all_by_ids(self, repo, session):
        items = await repo.save_all([Item(name="a"), Item(name="b"), Item(name="c")])
        ids = [item.id for item in items]

        deleted = await repo.delete_all(ids[:2])
        assert deleted == 2

        remaining = await repo.find_all()
        assert len(remaining) == 1
        assert remaining[0].name == "c"

    @pytest.mark.asyncio
    async def test_delete_all_by_ids_empty(self, repo):
        deleted = await repo.delete_all([])
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_all_entities(self, repo, session):
        items = await repo.save_all([Item(name="a"), Item(name="b")])
        deleted = await repo.delete_all_entities(items)
        assert deleted == 2
        assert await repo.count() == 0
