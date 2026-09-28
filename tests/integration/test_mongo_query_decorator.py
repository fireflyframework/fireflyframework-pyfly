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
"""@query methods on MongoDB repositories, executed on a real replica set: find filters, aggregation pipelines,
and @query beside derived methods."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.query import query
from tests.support.mongo import beanie_database

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class QDItem(BaseDocument):
    name: str
    role: str = "user"
    active: bool = True
    score: int = 0
    category: str = "general"

    class Settings:
        name = "qd_test_items"


# ---------------------------------------------------------------------------
# Test repositories
# ---------------------------------------------------------------------------


class QueryDecoratedRepo(MongoRepository[QDItem, str]):
    """Repository with @query-decorated methods (find filters)."""

    @query('{"role": ":role"}')
    async def find_by_role_query(self, role: str) -> list[QDItem]: ...

    @query('{"role": ":role", "active": true}')
    async def find_active_by_role(self, role: str) -> list[QDItem]: ...

    @query('{"score": {"$gte": ":min_score"}}')
    async def find_by_min_score(self, min_score: int) -> list[QDItem]: ...


class AggregateQueryRepo(MongoRepository[QDItem, str]):
    """Repository with @query-decorated aggregation pipeline methods."""

    @query('[{"$match": {"role": ":role"}}, {"$group": {"_id": "$category", "count": {"$sum": 1}}}]')
    async def count_by_role_grouped(self, role: str) -> list[dict]: ...

    @query('[{"$match": {"active": true}}]')
    async def find_active_via_pipeline(self) -> list[dict]: ...


class MixedQueryRepo(MongoRepository[QDItem, str]):
    """Repository with both @query-decorated and derived query methods."""

    @query('{"role": ":role"}')
    async def find_by_role_query(self, role: str) -> list[QDItem]: ...

    async def find_by_name(self, name: str) -> list[QDItem]: ...

    async def find_by_active(self, active: bool) -> list[QDItem]: ...


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db(mongo_rs_url: str) -> AsyncIterator[None]:
    """Bind the documents to a database of the test's own on the MongoDB replica set."""
    async with beanie_database(mongo_rs_url, [QDItem]):
        yield


@pytest.fixture
def processor():
    return MongoRepositoryBeanPostProcessor()


async def _seed() -> list[QDItem]:
    """Seed the database with known test data."""
    entities = [
        QDItem(name="Alice", role="admin", active=True, score=90, category="engineering"),
        QDItem(name="Bob", role="user", active=True, score=75, category="engineering"),
        QDItem(name="Carol", role="admin", active=False, score=85, category="marketing"),
        QDItem(name="Dave", role="user", active=False, score=60, category="marketing"),
    ]
    for e in entities:
        await e.save()
    return entities


# ===========================================================================
# 3. @query find filters — integration tests
# ===========================================================================


class TestQueryFindFilters:
    """@query methods with find filters are compiled and wired correctly."""

    @pytest.mark.asyncio
    async def test_simple_find_by_role(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """3a. Simple find filter returns matching documents."""
        await _seed()
        repo = QueryDecoratedRepo(QDItem)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_by_role_query(role="admin")
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_compound_find_filter(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """3b. Compound filter with static boolean + param returns correct results."""
        await _seed()
        repo = QueryDecoratedRepo(QDItem)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_active_by_role(role="admin")
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_find_with_operator(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """3c. Find filter with $gte operator preserves int type."""
        await _seed()
        repo = QueryDecoratedRepo(QDItem)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_by_min_score(min_score=80)
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_find_no_matches(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """3d. Find filter returns empty list when no documents match."""
        await _seed()
        repo = QueryDecoratedRepo(QDItem)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_by_role_query(role="nonexistent")
        assert results == []


# ===========================================================================
# 4. @query aggregation pipelines — integration tests
# ===========================================================================


class TestQueryAggregationPipelines:
    """@query methods with aggregation pipelines are compiled and wired correctly."""

    @pytest.mark.asyncio
    async def test_aggregate_group_by(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """4a. Aggregation pipeline with $match and $group returns grouped results."""
        await _seed()
        repo = AggregateQueryRepo(QDItem)
        processor.after_init(repo, "aggRepo")

        results = await repo.count_by_role_grouped(role="admin")
        # Two admins: Alice (engineering) and Carol (marketing)
        by_category = {r["_id"]: r["count"] for r in results}
        assert by_category == {"engineering": 1, "marketing": 1}

    @pytest.mark.asyncio
    async def test_aggregate_no_params(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """4b. Aggregation pipeline with no params works."""
        await _seed()
        repo = AggregateQueryRepo(QDItem)
        processor.after_init(repo, "aggRepo")

        results = await repo.find_active_via_pipeline()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_aggregate_no_matches(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """4c. Aggregation pipeline with no matching documents returns empty list."""
        await _seed()
        repo = AggregateQueryRepo(QDItem)
        processor.after_init(repo, "aggRepo")

        results = await repo.count_by_role_grouped(role="nonexistent")
        assert results == []


# ===========================================================================
# 5. Mixed @query and derived query methods coexist
# ===========================================================================


class TestMixedQueryAndDerived:
    """Both @query and derived query methods work on the same repository."""

    @pytest.mark.asyncio
    async def test_query_and_derived_coexist(self, processor: MongoRepositoryBeanPostProcessor) -> None:
        """5a. Both @query and derived methods work on the same repo."""
        await _seed()
        repo = MixedQueryRepo(QDItem)
        processor.after_init(repo, "mixedRepo")

        # @query method (uses **kwargs)
        by_role = await repo.find_by_role_query(role="admin")
        role_names = sorted(r.name for r in by_role)
        assert role_names == ["Alice", "Carol"]

        # Derived method (uses *args)
        by_name = await repo.find_by_name("Bob")
        assert len(by_name) == 1
        assert by_name[0].name == "Bob"

        # Another derived method
        by_active = await repo.find_by_active(False)
        inactive_names = sorted(r.name for r in by_active)
        assert inactive_names == ["Carol", "Dave"]
