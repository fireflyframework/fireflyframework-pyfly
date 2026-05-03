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
"""Tests for Generic Repository[T, ID] and @reactive_transactional."""

from uuid import UUID

import pytest
from sqlalchemy import Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.relational.sqlalchemy.transactional import reactive_transactional


class Item(BaseEntity):
    __tablename__ = "items"

    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(default=0.0)


class IntItem(Base):
    """Entity with an autoincrement integer primary key."""

    __tablename__ = "int_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[float] = mapped_column(default=0.0)


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
    return Repository(Item, session)


class TestRepository:
    @pytest.mark.asyncio
    async def test_save_and_find_by_id(self, repo: Repository[Item, UUID], session: AsyncSession):
        item = Item(name="Widget", price=9.99)
        saved = await repo.save(item)
        assert isinstance(saved.id, UUID)

        found = await repo.find_by_id(saved.id)
        assert found is not None
        assert found.name == "Widget"
        assert found.price == 9.99

    @pytest.mark.asyncio
    async def test_find_by_id_not_found(self, repo: Repository[Item, UUID]):
        from uuid import uuid4

        result = await repo.find_by_id(uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_find_all(self, repo: Repository[Item, UUID]):
        await repo.save(Item(name="A", price=1.0))
        await repo.save(Item(name="B", price=2.0))
        await repo.save(Item(name="C", price=3.0))

        items = await repo.find_all()
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_delete(self, repo: Repository[Item, UUID]):
        item = await repo.save(Item(name="ToDelete", price=0.0))
        await repo.delete(item.id)

        result = await repo.find_by_id(item.id)
        assert result is None

    @pytest.mark.asyncio
    async def test_find_paginated(self, repo: Repository[Item, UUID]):
        for i in range(15):
            await repo.save(Item(name=f"Item-{i}", price=float(i)))

        page = await repo.find_paginated(page=1, size=5)
        assert isinstance(page, Page)
        assert len(page.items) == 5
        assert page.total == 15
        assert page.total_pages == 3
        assert page.has_next is True

    @pytest.mark.asyncio
    async def test_find_paginated_last_page(self, repo: Repository[Item, UUID]):
        for i in range(12):
            await repo.save(Item(name=f"Item-{i}", price=float(i)))

        page = await repo.find_paginated(page=3, size=5)
        assert len(page.items) == 2
        assert page.total == 12
        assert page.has_next is False
        assert page.has_previous is True

    @pytest.mark.asyncio
    async def test_count(self, repo: Repository[Item, UUID]):
        await repo.save(Item(name="A"))
        await repo.save(Item(name="B"))
        assert await repo.count() == 2

    @pytest.mark.asyncio
    async def test_exists(self, repo: Repository[Item, UUID]):
        item = await repo.save(Item(name="Exists"))
        assert await repo.exists(item.id) is True

        from uuid import uuid4

        assert await repo.exists(uuid4()) is False


class TestReactiveTransactional:
    @pytest.mark.asyncio
    async def test_commits_on_success(self, session_factory):
        @reactive_transactional(session_factory)
        async def create_item(session: AsyncSession) -> Item:
            item = Item(name="Transactional", price=42.0)
            session.add(item)
            return item

        item = await create_item()
        assert item.name == "Transactional"

        # Verify it was committed
        async with session_factory() as verify_session:
            result = await verify_session.execute(select(Item).where(Item.id == item.id))
            found = result.scalar_one_or_none()
            assert found is not None
            assert found.name == "Transactional"

    @pytest.mark.asyncio
    async def test_rolls_back_on_error(self, session_factory):
        @reactive_transactional(session_factory)
        async def failing_operation(session: AsyncSession) -> None:
            session.add(Item(name="WillFail", price=0.0))
            await session.flush()
            raise ValueError("Simulated failure")

        with pytest.raises(ValueError, match="Simulated failure"):
            await failing_operation()

        # Verify nothing was committed
        async with session_factory() as verify_session:
            result = await verify_session.execute(select(Item))
            items = result.scalars().all()
            assert len(items) == 0


class TestRepositorySpecification:
    @pytest.mark.asyncio
    async def test_find_all_by_spec(self, repo):
        await repo.save(Item(name="A", price=10.0))
        await repo.save(Item(name="B", price=20.0))
        await repo.save(Item(name="C", price=30.0))

        expensive = Specification(lambda root, q: q.where(root.price > 15.0))
        items = await repo.find_all_by_spec(expensive)
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_find_all_by_spec_and(self, repo):
        await repo.save(Item(name="A", price=10.0))
        await repo.save(Item(name="B", price=20.0))
        await repo.save(Item(name="A", price=30.0))

        named_a = Specification(lambda root, q: q.where(root.name == "A"))
        expensive = Specification(lambda root, q: q.where(root.price > 15.0))
        items = await repo.find_all_by_spec(named_a & expensive)
        assert len(items) == 1
        assert items[0].price == 30.0

    @pytest.mark.asyncio
    async def test_find_all_by_spec_paged(self, repo):
        for i in range(20):
            await repo.save(Item(name=f"Item-{i}", price=float(i)))

        expensive = Specification(lambda root, q: q.where(root.price >= 10.0))
        pageable = Pageable.of(1, 5, Sort.by("name"))
        page = await repo.find_all_by_spec_paged(expensive, pageable)

        assert isinstance(page, Page)
        assert page.total == 10  # Items 10-19
        assert len(page.items) == 5
        assert page.has_next is True

    @pytest.mark.asyncio
    async def test_find_all_by_spec_paged_with_sort(self, repo):
        await repo.save(Item(name="C", price=30.0))
        await repo.save(Item(name="A", price=10.0))
        await repo.save(Item(name="B", price=20.0))

        all_spec = Specification(lambda root, q: q)
        pageable = Pageable.of(1, 10, Sort(orders=(Order.desc("price"),)))
        page = await repo.find_all_by_spec_paged(all_spec, pageable)

        assert [item.price for item in page.items] == [30.0, 20.0, 10.0]

    @pytest.mark.asyncio
    async def test_find_all_by_spec_empty_result(self, repo):
        await repo.save(Item(name="A", price=10.0))

        impossible = Specification(lambda root, q: q.where(root.price > 999.0))
        items = await repo.find_all_by_spec(impossible)
        assert items == []


class TestInitSubclass:
    """Tests for __init_subclass__ entity type extraction."""

    def test_extracts_entity_type(self):
        class ItemRepo(Repository[Item, UUID]):
            pass

        assert ItemRepo._entity_type is Item
        assert ItemRepo._id_type is UUID

    def test_extracts_int_id_type(self):
        class IntItemRepo(Repository[IntItem, int]):
            pass

        assert IntItemRepo._entity_type is IntItem
        assert IntItemRepo._id_type is int

    def test_unparameterized_subclass_has_none(self):
        class BaseRepo(Repository):
            pass

        assert BaseRepo._entity_type is None
        assert BaseRepo._id_type is None

    @pytest.mark.asyncio
    async def test_optional_model_uses_entity_type(self, session):
        class ItemRepo(Repository[Item, UUID]):
            pass

        repo = ItemRepo(session=session)
        assert repo._model is Item

    @pytest.mark.asyncio
    async def test_explicit_model_takes_precedence(self, session):
        class ItemRepo(Repository[Item, UUID]):
            pass

        repo = ItemRepo(model=IntItem, session=session)
        assert repo._model is IntItem

    def test_no_model_no_generic_raises(self):
        class BareRepo(Repository):
            pass

        with pytest.raises(TypeError, match="requires either"):
            BareRepo()


class TestRepositoryIntId:
    """Tests for Repository[T, int] with autoincrement integer primary keys."""

    @pytest.fixture
    async def int_engine(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def int_session_factory(self, int_engine):
        return async_sessionmaker(int_engine, expire_on_commit=False)

    @pytest.fixture
    async def int_session(self, int_session_factory):
        async with int_session_factory() as session:
            yield session

    @pytest.fixture
    def int_repo(self, int_session):
        return Repository(IntItem, int_session)

    @pytest.mark.asyncio
    async def test_save_and_find_by_int_id(self, int_repo: Repository[IntItem, int]):
        item = IntItem(name="Widget", price=9.99)
        saved = await int_repo.save(item)
        assert isinstance(saved.id, int)
        assert saved.id > 0

        found = await int_repo.find_by_id(saved.id)
        assert found is not None
        assert found.name == "Widget"

    @pytest.mark.asyncio
    async def test_find_by_int_id_not_found(self, int_repo: Repository[IntItem, int]):
        result = await int_repo.find_by_id(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_find_all_int_id(self, int_repo: Repository[IntItem, int]):
        await int_repo.save(IntItem(name="A", price=1.0))
        await int_repo.save(IntItem(name="B", price=2.0))
        items = await int_repo.find_all()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_delete_int_id(self, int_repo: Repository[IntItem, int]):
        item = await int_repo.save(IntItem(name="ToDelete", price=0.0))
        await int_repo.delete(item.id)
        assert await int_repo.find_by_id(item.id) is None

    @pytest.mark.asyncio
    async def test_count_int_id(self, int_repo: Repository[IntItem, int]):
        await int_repo.save(IntItem(name="A"))
        await int_repo.save(IntItem(name="B"))
        assert await int_repo.count() == 2

    @pytest.mark.asyncio
    async def test_exists_int_id(self, int_repo: Repository[IntItem, int]):
        item = await int_repo.save(IntItem(name="Exists"))
        assert await int_repo.exists(item.id) is True
        assert await int_repo.exists(999) is False

    @pytest.mark.asyncio
    async def test_autoincrement_ids(self, int_repo: Repository[IntItem, int]):
        a = await int_repo.save(IntItem(name="First"))
        b = await int_repo.save(IntItem(name="Second"))
        assert b.id == a.id + 1
