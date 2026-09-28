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
"""@query methods on MongoDB repositories, executed on a real replica set: find filters, aggregation pipelines
(pipelines that write with ``$out`` or ``$merge`` included), and @query beside derived methods."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.query import MongoAnnotatedQuery
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.query import query
from pyfly.data.transaction import IllegalTransactionStateError, Propagation, TransactionTemplate
from tests.support.mongo import BeanieDatabase, beanie_database

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


class WritePipelineRepo(MongoRepository[QDItem, str]):
    """Repository with @query pipelines that write another collection."""

    @query('[{"$match": {"score": {"$gte": ":min_score"}}}, {"$project": {"name": 1}}, {"$out": "qd_high_scores"}]')
    async def copy_high_scores(self, min_score: int) -> list[dict]: ...

    @query('[{"$match": {"role": ":role"}}, {"$project": {"name": 1}}, {"$merge": {"into": "qd_by_role"}}]')
    async def merge_role(self, role: str) -> list[dict]: ...


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
async def init_db(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    """Bind the documents to a database of the test's own on the MongoDB replica set."""
    async with beanie_database(mongo_rs_url, [QDItem]) as db:
        yield db


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
# 4b. @query pipelines that write ($out, $merge)
# ===========================================================================


async def _names_in(db: BeanieDatabase, collection: str) -> list[str]:
    """The names a pipeline wrote to *collection*, read on the test's client outside every session."""
    return sorted(row["name"] for row in await db.database[collection].find({}).to_list())


class TestQueryWritePipelines:
    """A pipeline ending in ``$out`` or ``$merge`` is one command: outside a transaction it runs without one
    (MongoDB refuses both stages in a multi-document transaction), and inside one it fails before it is sent."""

    @pytest.mark.asyncio
    async def test_out_pipeline_writes_its_collection_outside_a_transaction(
        self, processor: MongoRepositoryBeanPostProcessor, init_db: BeanieDatabase
    ) -> None:
        await _seed()
        repo = WritePipelineRepo(QDItem)
        processor.after_init(repo, "writeRepo")

        assert await repo.copy_high_scores(80) == []
        assert await _names_in(init_db, "qd_high_scores") == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_merge_pipeline_writes_its_collection_outside_a_transaction(
        self, processor: MongoRepositoryBeanPostProcessor, init_db: BeanieDatabase
    ) -> None:
        await _seed()
        repo = WritePipelineRepo(QDItem)
        processor.after_init(repo, "writeRepo")

        assert await repo.merge_role(role="user") == []
        assert await repo.merge_role(role="admin") == []
        assert await _names_in(init_db, "qd_by_role") == ["Alice", "Bob", "Carol", "Dave"]

    @pytest.mark.asyncio
    async def test_a_compiled_write_pipeline_called_with_the_document_class_runs_outside_a_transaction(
        self, init_db: BeanieDatabase
    ) -> None:
        await _seed()
        compiled = MongoAnnotatedQuery([{"$match": {"active": ":active"}}, {"$out": "qd_active"}])

        assert await compiled(QDItem, active=False) == []
        assert await _names_in(init_db, "qd_active") == ["Carol", "Dave"]

    @pytest.mark.asyncio
    async def test_a_write_pipeline_inside_a_transaction_fails_before_it_is_sent(
        self, processor: MongoRepositoryBeanPostProcessor, init_db: BeanieDatabase
    ) -> None:
        await _seed()
        repo = WritePipelineRepo(QDItem)
        processor.after_init(repo, "writeRepo")
        template = TransactionTemplate(MongoTransactionManager.for_client(init_db.client))

        async with template.transaction():
            await repo.save(QDItem(name="Erin", score=99))
            init_db.log.clear()
            with pytest.raises(IllegalTransactionStateError, match=r"\$out or \$merge"):
                await repo.copy_high_scores(80)
            with pytest.raises(IllegalTransactionStateError, match=r"\$out or \$merge"):
                await repo.merge_role(role="admin")
            assert init_db.log.names() == []  # refused before any command reached the server

        # The refusal left the transaction usable: the write before it committed, and no pipeline ran.
        assert "Erin" in await _names_in(init_db, "qd_test_items")
        assert "qd_high_scores" not in await init_db.database.list_collection_names()
        assert "qd_by_role" not in await init_db.database.list_collection_names()

    @pytest.mark.asyncio
    async def test_a_write_pipeline_runs_in_a_not_supported_boundary_inside_a_transaction(
        self, processor: MongoRepositoryBeanPostProcessor, init_db: BeanieDatabase
    ) -> None:
        await _seed()
        repo = WritePipelineRepo(QDItem)
        processor.after_init(repo, "writeRepo")
        template = TransactionTemplate(MongoTransactionManager.for_client(init_db.client))

        async with template.transaction():
            await repo.save(QDItem(name="Erin", score=99))
            async with template.transaction(propagation=Propagation.NOT_SUPPORTED):
                await repo.copy_high_scores(80)  # suspended transaction: the pipeline cannot see Erin yet

        assert await _names_in(init_db, "qd_high_scores") == ["Alice", "Carol"]
        assert "Erin" in await _names_in(init_db, "qd_test_items")


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
