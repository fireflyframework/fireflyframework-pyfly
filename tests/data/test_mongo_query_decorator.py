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
"""Tests for @query decorator support on MongoDB repositories that need no database: placeholders, compiling,
the shared decorator. Its execution is proven on a real replica set in
``tests/integration/test_mongo_query_decorator.py``."""

from __future__ import annotations

import pytest

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.query import MongoQueryExecutor, _substitute_params
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
