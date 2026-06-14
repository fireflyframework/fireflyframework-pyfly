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
"""Tests for MongoRepository — integration tests using mongomock."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.pageable import Pageable, Sort

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class SampleItem(BaseDocument):
    name: str
    description: str = ""
    active: bool = True

    class Settings:
        name = "test_items"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db():
    """Initialise Beanie with an in-memory mock client."""
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[SampleItem])
    yield
    client.close()


@pytest.fixture
def repo():
    return MongoRepository[SampleItem, str](SampleItem)


# ===========================================================================
# 1. CRUD operations
# ===========================================================================


class TestInitSubclass:
    """Tests for __init_subclass__ entity type extraction."""

    def test_extracts_entity_type(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        assert SampleItemRepo._entity_type is SampleItem
        assert SampleItemRepo._id_type is str

    def test_unparameterized_subclass_has_none(self):
        class BaseRepo(MongoRepository):
            pass

        assert BaseRepo._entity_type is None
        assert BaseRepo._id_type is None

    def test_optional_model_uses_entity_type(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        repo = SampleItemRepo()
        assert repo._model is SampleItem

    def test_explicit_model_takes_precedence(self):
        class SampleItemRepo(MongoRepository[SampleItem, str]):
            pass

        class OtherDoc(BaseDocument):
            name: str

            class Settings:
                name = "other"

        repo = SampleItemRepo(model=OtherDoc)
        assert repo._model is OtherDoc

    def test_no_model_no_generic_raises(self):
        class BareRepo(MongoRepository):
            pass

        with pytest.raises(TypeError, match="requires either"):
            BareRepo()


class TestCRUD:
    """Basic CRUD operations."""

    @pytest.mark.asyncio
    async def test_save_and_find_by_id(self, repo: MongoRepository):
        item = SampleItem(name="Widget", description="A test widget")
        saved = await repo.save(item)
        assert saved.id is not None

        found = await repo.find_by_id(saved.id)
        assert found is not None
        assert found.name == "Widget"

    @pytest.mark.asyncio
    async def test_find_all(self, repo: MongoRepository):
        await repo.save(SampleItem(name="A"))
        await repo.save(SampleItem(name="B"))
        await repo.save(SampleItem(name="C"))

        items = await repo.find_all()
        assert len(items) == 3

    @pytest.mark.asyncio
    async def test_find_all_with_filters(self, repo: MongoRepository):
        await repo.save(SampleItem(name="Active", active=True))
        await repo.save(SampleItem(name="Inactive", active=False))

        active_items = await repo.find_all(active=True)
        assert len(active_items) == 1
        assert active_items[0].name == "Active"

    @pytest.mark.asyncio
    async def test_delete_by_id(self, repo: MongoRepository):
        item = await repo.save(SampleItem(name="ToDelete"))
        await repo.delete_by_id(item.id)

        found = await repo.find_by_id(item.id)
        assert found is None

    @pytest.mark.asyncio
    async def test_delete_entity(self, repo: MongoRepository):
        item = await repo.save(SampleItem(name="ToDelete"))
        await repo.delete(item)

        assert await repo.find_by_id(item.id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_no_error(self, repo: MongoRepository):
        """Deleting a non-existent ID should not raise."""
        from bson import ObjectId

        await repo.delete_by_id(ObjectId())

    @pytest.mark.asyncio
    async def test_find_by_id_nonexistent_returns_none(self, repo: MongoRepository):
        from bson import ObjectId

        found = await repo.find_by_id(ObjectId())
        assert found is None


# ===========================================================================
# 2. Count and exists
# ===========================================================================


class TestCountAndExists:
    @pytest.mark.asyncio
    async def test_count_empty(self, repo: MongoRepository):
        assert await repo.count() == 0

    @pytest.mark.asyncio
    async def test_count_with_items(self, repo: MongoRepository):
        await repo.save(SampleItem(name="A"))
        await repo.save(SampleItem(name="B"))
        assert await repo.count() == 2

    @pytest.mark.asyncio
    async def test_exists_by_id_true(self, repo: MongoRepository):
        item = await repo.save(SampleItem(name="Exists"))
        assert await repo.exists_by_id(item.id) is True

    @pytest.mark.asyncio
    async def test_exists_by_id_false(self, repo: MongoRepository):
        from bson import ObjectId

        assert await repo.exists_by_id(ObjectId()) is False


# ===========================================================================
# 3. Pagination
# ===========================================================================


class TestPagination:
    @pytest.mark.asyncio
    async def test_find_all_pageable_basic(self, repo: MongoRepository):
        for i in range(15):
            await repo.save(SampleItem(name=f"Item-{i:02d}"))

        page = await repo.find_all(Pageable.of(1, 5))
        assert len(page.items) == 5
        assert page.total == 15
        assert page.page == 1
        assert page.size == 5

    @pytest.mark.asyncio
    async def test_find_all_pageable_second_page(self, repo: MongoRepository):
        for i in range(12):
            await repo.save(SampleItem(name=f"Item-{i:02d}"))

        page = await repo.find_all(Pageable.of(2, 5))
        assert len(page.items) == 5
        assert page.total == 12
        assert page.page == 2

    @pytest.mark.asyncio
    async def test_find_all_pageable_last_page_partial(self, repo: MongoRepository):
        for i in range(7):
            await repo.save(SampleItem(name=f"Item-{i:02d}"))

        page = await repo.find_all(Pageable.of(2, 5))
        assert len(page.items) == 2
        assert page.total == 7

    @pytest.mark.asyncio
    async def test_find_all_pageable_empty(self, repo: MongoRepository):
        page = await repo.find_all(Pageable.of(1, 10))
        assert len(page.items) == 0
        assert page.total == 0

    @pytest.mark.asyncio
    async def test_find_all_sorted(self, repo: MongoRepository):
        for n in ("C", "A", "B"):
            await repo.save(SampleItem(name=n))
        names = [w.name for w in await repo.find_all(Sort.by("name"))]
        assert names == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_stream_all(self, repo: MongoRepository):
        for n in ("C", "A", "B"):
            await repo.save(SampleItem(name=n))
        names = [w.name async for w in repo.stream_all(Sort.by("name"))]
        assert names == ["A", "B", "C"]
