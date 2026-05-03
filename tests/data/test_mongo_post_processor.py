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
"""Tests for MongoRepositoryBeanPostProcessor — integration tests using mongomock."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class PPItem(BaseDocument):
    name: str
    role: str = "user"
    active: bool = True

    class Settings:
        name = "pp_test_items"


# ---------------------------------------------------------------------------
# Test repositories
# ---------------------------------------------------------------------------


class DerivedQueryRepo(MongoRepository[PPItem, str]):
    """Repository with derived query methods."""

    async def find_by_name(self, name: str) -> list[PPItem]: ...

    async def find_by_active(self, active: bool) -> list[PPItem]: ...


class AllDerivedTypesRepo(MongoRepository[PPItem, str]):
    """Repository with all four derived query prefixes."""

    async def find_by_name(self, name: str) -> list[PPItem]: ...

    async def count_by_active(self, active: bool) -> int: ...

    async def exists_by_role(self, role: str) -> bool: ...

    async def delete_by_name(self, name: str) -> int: ...


class ConcreteMethodRepo(MongoRepository[PPItem, str]):
    """Repository with a concrete (non-stub) find_by_ method."""

    async def find_by_name(self, name: str) -> list[PPItem]:
        return await self._model.find({"name": name}).to_list()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[PPItem])
    yield
    client.close()


@pytest.fixture
def processor():
    return MongoRepositoryBeanPostProcessor()


async def _seed() -> list[PPItem]:
    """Seed the database with known test data."""
    entities = [
        PPItem(name="Alice", role="admin", active=True),
        PPItem(name="Bob", role="user", active=True),
        PPItem(name="Carol", role="admin", active=False),
        PPItem(name="Dave", role="user", active=False),
    ]
    for e in entities:
        await e.save()
    return entities


# ===========================================================================
# 1. Non-repository beans pass through
# ===========================================================================


class TestNonRepositoryPassThrough:
    def test_plain_object_passes_through(self, processor: MongoRepositoryBeanPostProcessor):
        class PlainBean:
            value = 42

        bean = PlainBean()
        result = processor.after_init(bean, "plainBean")
        assert result is bean
        assert result.value == 42

    def test_before_init_returns_unchanged(self, processor: MongoRepositoryBeanPostProcessor):
        class AnyBean:
            pass

        bean = AnyBean()
        result = processor.before_init(bean, "anyBean")
        assert result is bean


# ===========================================================================
# 2. Derived query methods get wired
# ===========================================================================


class TestDerivedQueryMethods:
    @pytest.mark.asyncio
    async def test_find_by_name(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_name("Alice")
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_find_by_active(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_active(True)
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_find_by_no_matches(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_name("Nonexistent")
        assert results == []


# ===========================================================================
# 3. All derived query types work
# ===========================================================================


class TestAllDerivedQueryTypes:
    @pytest.mark.asyncio
    async def test_count_by(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = AllDerivedTypesRepo(PPItem)
        processor.after_init(repo, "allTypesRepo")

        count = await repo.count_by_active(True)
        assert count == 2

    @pytest.mark.asyncio
    async def test_exists_by_true(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = AllDerivedTypesRepo(PPItem)
        processor.after_init(repo, "allTypesRepo")

        assert await repo.exists_by_role("admin") is True

    @pytest.mark.asyncio
    async def test_exists_by_false(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = AllDerivedTypesRepo(PPItem)
        processor.after_init(repo, "allTypesRepo")

        assert await repo.exists_by_role("superuser") is False

    @pytest.mark.asyncio
    async def test_delete_by(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = AllDerivedTypesRepo(PPItem)
        processor.after_init(repo, "allTypesRepo")

        deleted_count = await repo.delete_by_name("Alice")
        assert deleted_count == 1

        # Verify deletion
        remaining = await PPItem.find({"name": "Alice"}).to_list()
        assert len(remaining) == 0


# ===========================================================================
# 4. Base methods preserved
# ===========================================================================


class TestBaseMethodsPreserved:
    @pytest.mark.asyncio
    async def test_save_still_works(self, processor: MongoRepositoryBeanPostProcessor):
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        item = PPItem(name="NewItem", role="tester", active=True)
        saved = await repo.save(item)
        assert saved.name == "NewItem"
        assert saved.id is not None

    @pytest.mark.asyncio
    async def test_find_all_still_works(self, processor: MongoRepositoryBeanPostProcessor):
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        await repo.save(PPItem(name="X", role="user"))
        await repo.save(PPItem(name="Y", role="user"))

        items = await repo.find_all()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_count_still_works(self, processor: MongoRepositoryBeanPostProcessor):
        repo = DerivedQueryRepo(PPItem)
        processor.after_init(repo, "derivedRepo")

        await repo.save(PPItem(name="A", role="user"))
        total = await repo.count()
        assert total == 1


# ===========================================================================
# 5. Concrete methods are NOT replaced
# ===========================================================================


class TestConcreteMethodsPreserved:
    @pytest.mark.asyncio
    async def test_concrete_find_by_not_replaced(self, processor: MongoRepositoryBeanPostProcessor):
        await _seed()
        repo = ConcreteMethodRepo(PPItem)
        processor.after_init(repo, "concreteRepo")

        results = await repo.find_by_name("Alice")
        assert len(results) == 1
        assert results[0].name == "Alice"
