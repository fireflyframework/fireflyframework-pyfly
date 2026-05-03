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
"""Tests for @query decorator support on MongoDB repositories."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.query import MongoQueryExecutor, _substitute_params
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.query import query

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
async def init_db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[QDItem])
    yield
    client.close()


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
# 1. _substitute_params unit tests
# ===========================================================================


class TestSubstituteParams:
    """Unit tests for the parameter substitution function."""

    def test_exact_string_replacement(self) -> None:
        """1a. Exact ':param' string values are replaced with the actual value."""
        doc = {"email": ":email"}
        result = _substitute_params(doc, {"email": "alice@test.com"})
        assert result == {"email": "alice@test.com"}

    def test_integer_type_preserved(self) -> None:
        """1b. Integer values are preserved, not stringified."""
        doc = {"score": {"$gte": ":min_score"}}
        result = _substitute_params(doc, {"min_score": 80})
        assert result == {"score": {"$gte": 80}}
        assert isinstance(result["score"]["$gte"], int)

    def test_boolean_type_preserved(self) -> None:
        """1c. Boolean values are preserved."""
        doc = {"active": ":is_active"}
        result = _substitute_params(doc, {"is_active": True})
        assert result == {"active": True}
        assert isinstance(result["active"], bool)

    def test_list_type_preserved(self) -> None:
        """1d. List values are preserved for $in operators."""
        doc = {"role": {"$in": ":roles"}}
        result = _substitute_params(doc, {"roles": ["admin", "user"]})
        assert result == {"role": {"$in": ["admin", "user"]}}

    def test_no_substitution_for_non_placeholder_strings(self) -> None:
        """1e. String values without ':' prefix are left unchanged."""
        doc = {"active": True, "status": "enabled"}
        result = _substitute_params(doc, {"email": "test@test.com"})
        assert result == {"active": True, "status": "enabled"}

    def test_nested_dict_substitution(self) -> None:
        """1f. Nested dicts are recursed into."""
        doc = {"$and": [{"role": ":role"}, {"active": ":active"}]}
        result = _substitute_params(doc, {"role": "admin", "active": True})
        assert result == {"$and": [{"role": "admin"}, {"active": True}]}

    def test_pipeline_substitution(self) -> None:
        """1g. Aggregation pipelines (lists of dicts) are substituted correctly."""
        pipeline = [{"$match": {"role": ":role"}}, {"$group": {"_id": "$category"}}]
        result = _substitute_params(pipeline, {"role": "admin"})
        assert result == [{"$match": {"role": "admin"}}, {"$group": {"_id": "$category"}}]

    def test_non_string_values_pass_through(self) -> None:
        """1h. Non-string values (int, bool, None) pass through unchanged."""
        doc = {"count": 5, "active": True, "deleted": None}
        result = _substitute_params(doc, {})
        assert result == {"count": 5, "active": True, "deleted": None}

    def test_embedded_placeholder_in_string(self) -> None:
        """1i. Partial placeholder within a longer string is interpolated."""
        doc = {"name": {"$regex": "^:prefix"}}
        result = _substitute_params(doc, {"prefix": "Al"})
        assert result == {"name": {"$regex": "^Al"}}


# ===========================================================================
# 2. MongoQueryExecutor unit tests
# ===========================================================================


class TestMongoQueryExecutor:
    """Unit tests for MongoQueryExecutor compile-time validation."""

    def test_compile_raises_without_decorator(self) -> None:
        """2a. compile_query_method raises if method lacks __pyfly_query__."""
        executor = MongoQueryExecutor()

        async def plain_method(self: object) -> list[QDItem]: ...

        with pytest.raises(AttributeError, match="__pyfly_query__"):
            executor.compile_query_method(plain_method, QDItem)

    def test_compile_raises_on_invalid_json(self) -> None:
        """2b. compile_query_method raises on invalid JSON."""
        executor = MongoQueryExecutor()

        @query("not valid json")
        async def bad_method(self: object) -> list[QDItem]: ...

        with pytest.raises(ValueError):
            executor.compile_query_method(bad_method, QDItem)

    def test_compile_find_filter(self) -> None:
        """2c. compile_query_method returns a callable for find filters."""
        executor = MongoQueryExecutor()

        @query('{"role": ":role"}')
        async def find_method(self: object, role: str) -> list[QDItem]: ...

        compiled = executor.compile_query_method(find_method, QDItem)
        assert callable(compiled)

    def test_compile_aggregation_pipeline(self) -> None:
        """2d. compile_query_method returns a callable for aggregation pipelines."""
        executor = MongoQueryExecutor()

        @query('[{"$match": {"role": ":role"}}]')
        async def agg_method(self: object, role: str) -> list[dict]: ...

        compiled = executor.compile_query_method(agg_method, QDItem)
        assert callable(compiled)


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


# ===========================================================================
# 6. Shared @query decorator tests
# ===========================================================================


class TestSharedQueryDecorator:
    """The shared @query decorator works from all import paths."""

    def test_decorator_stamps_metadata(self) -> None:
        """6a. @query stamps __pyfly_query__ and __pyfly_query_native__ on the function."""

        @query('{"email": ":email"}')
        async def find_method(self: object, email: str) -> list[QDItem]: ...

        assert hasattr(find_method, "__pyfly_query__")
        assert find_method.__pyfly_query__ == '{"email": ":email"}'
        assert find_method.__pyfly_query_native__ is False

    def test_decorator_native_flag(self) -> None:
        """6b. @query(native=True) sets __pyfly_query_native__ to True."""

        @query('{"email": ":email"}', native=True)
        async def find_method(self: object, email: str) -> list[QDItem]: ...

        assert find_method.__pyfly_query_native__ is True

    def test_import_from_shared_location(self) -> None:
        """6c. @query can be imported from pyfly.data.query."""
        from pyfly.data.query import query as shared_query

        assert shared_query is query

    def test_import_from_data_init(self) -> None:
        """6d. @query can be imported from pyfly.data."""
        from pyfly.data import query as data_query

        assert data_query is query

    def test_import_from_sqlalchemy_compat(self) -> None:
        """6e. @query can still be imported from pyfly.data.relational.sqlalchemy.query."""
        from pyfly.data.relational.sqlalchemy.query import query as sa_query

        assert sa_query is query
