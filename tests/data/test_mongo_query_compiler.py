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
"""Tests for MongoQueryMethodCompiler — unit tests, no MongoDB required."""

from __future__ import annotations

import pymongo
import pytest

from pyfly.data.document.mongodb.query_compiler import MongoQueryMethodCompiler
from pyfly.data.query_parser import FieldPredicate, OrderClause, ParsedQuery, QueryMethodParser

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def compiler():
    return MongoQueryMethodCompiler()


@pytest.fixture
def parser():
    return QueryMethodParser()


# ===========================================================================
# 1. _build_clause — single operator tests
# ===========================================================================


class TestBuildClause:
    """Test _build_clause for every supported operator."""

    def test_eq(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="name", operator="eq")
        clause, idx = compiler._build_clause("name", pred, ["Alice"], 0)
        assert clause == {"name": "Alice"}
        assert idx == 1

    def test_not(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="status", operator="not")
        clause, idx = compiler._build_clause("status", pred, ["inactive"], 0)
        assert clause == {"status": {"$ne": "inactive"}}
        assert idx == 1

    def test_gt(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="age", operator="gt")
        clause, idx = compiler._build_clause("age", pred, [18], 0)
        assert clause == {"age": {"$gt": 18}}
        assert idx == 1

    def test_gte(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="age", operator="gte")
        clause, idx = compiler._build_clause("age", pred, [21], 0)
        assert clause == {"age": {"$gte": 21}}
        assert idx == 1

    def test_lt(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="price", operator="lt")
        clause, idx = compiler._build_clause("price", pred, [100], 0)
        assert clause == {"price": {"$lt": 100}}
        assert idx == 1

    def test_lte(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="price", operator="lte")
        clause, idx = compiler._build_clause("price", pred, [50], 0)
        assert clause == {"price": {"$lte": 50}}
        assert idx == 1

    def test_like(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="name", operator="like")
        clause, idx = compiler._build_clause("name", pred, ["%Al%"], 0)
        assert "$regex" in clause["name"]
        assert idx == 1

    def test_containing(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="name", operator="containing")
        clause, idx = compiler._build_clause("name", pred, ["lic"], 0)
        assert clause["name"]["$regex"] == ".*lic.*"
        assert clause["name"]["$options"] == "i"
        assert idx == 1

    def test_in(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="role", operator="in")
        clause, idx = compiler._build_clause("role", pred, [["admin", "user"]], 0)
        assert clause == {"role": {"$in": ["admin", "user"]}}
        assert idx == 1

    def test_between(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="age", operator="between")
        clause, idx = compiler._build_clause("age", pred, [18, 65], 0)
        assert clause == {"age": {"$gte": 18, "$lte": 65}}
        assert idx == 2

    def test_is_null(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="email", operator="is_null")
        clause, idx = compiler._build_clause("email", pred, [], 0)
        assert clause == {"email": None}
        assert idx == 0

    def test_is_not_null(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="email", operator="is_not_null")
        clause, idx = compiler._build_clause("email", pred, [], 0)
        assert clause == {"email": {"$ne": None}}
        assert idx == 0

    def test_unknown_operator_raises(self, compiler: MongoQueryMethodCompiler):
        pred = FieldPredicate(field_name="x", operator="unknown_op")
        with pytest.raises(ValueError, match="Unknown operator"):
            compiler._build_clause("x", pred, [], 0)


# ===========================================================================
# 2. _build_filter — combined filter tests
# ===========================================================================


class TestBuildFilter:
    """Test _build_filter with various predicate combinations."""

    def test_single_predicate(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[FieldPredicate("name", "eq")],
        )
        result = compiler._build_filter(parsed, ["Alice"])
        assert result == {"name": "Alice"}

    def test_and_connectors(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[
                FieldPredicate("name", "eq"),
                FieldPredicate("active", "eq"),
            ],
            connectors=["and"],
        )
        result = compiler._build_filter(parsed, ["Alice", True])
        assert result == {"$and": [{"name": "Alice"}, {"active": True}]}

    def test_or_connectors(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[
                FieldPredicate("role", "eq"),
                FieldPredicate("role", "eq"),
            ],
            connectors=["or"],
        )
        result = compiler._build_filter(parsed, ["admin", "superadmin"])
        assert result == {"$or": [{"role": "admin"}, {"role": "superadmin"}]}

    def test_mixed_connectors(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[
                FieldPredicate("name", "eq"),
                FieldPredicate("role", "eq"),
                FieldPredicate("active", "eq"),
            ],
            connectors=["and", "or"],
        )
        result = compiler._build_filter(parsed, ["Alice", "admin", True])
        # Mixed: (name=Alice AND role=admin) OR active=True
        assert "$or" in result or "$and" in result

    def test_empty_predicates(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(prefix="find_by")
        result = compiler._build_filter(parsed, [])
        assert result == {}


# ===========================================================================
# 3. _build_sort
# ===========================================================================


class TestBuildSort:
    """Test _build_sort with various order clause combinations."""

    def test_single_asc(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            order_clauses=[OrderClause("name", "asc")],
        )
        result = compiler._build_sort(parsed)
        assert result == [("name", pymongo.ASCENDING)]

    def test_single_desc(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            order_clauses=[OrderClause("created_at", "desc")],
        )
        result = compiler._build_sort(parsed)
        assert result == [("created_at", pymongo.DESCENDING)]

    def test_multiple_orders(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            order_clauses=[
                OrderClause("name", "asc"),
                OrderClause("created_at", "desc"),
            ],
        )
        result = compiler._build_sort(parsed)
        assert result == [("name", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)]

    def test_empty_order(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(prefix="find_by")
        result = compiler._build_sort(parsed)
        assert result == []


# ===========================================================================
# 4. compile() dispatch
# ===========================================================================


class TestCompileDispatch:
    """Test that compile() returns callables for all prefixes."""

    def test_find_by_returns_callable(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[FieldPredicate("name", "eq")],
        )
        fn = compiler.compile(parsed, object)
        assert callable(fn)

    def test_count_by_returns_callable(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="count_by",
            predicates=[FieldPredicate("active", "eq")],
        )
        fn = compiler.compile(parsed, object)
        assert callable(fn)

    def test_exists_by_returns_callable(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="exists_by",
            predicates=[FieldPredicate("email", "eq")],
        )
        fn = compiler.compile(parsed, object)
        assert callable(fn)

    def test_delete_by_returns_callable(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(
            prefix="delete_by",
            predicates=[FieldPredicate("name", "eq")],
        )
        fn = compiler.compile(parsed, object)
        assert callable(fn)

    def test_unknown_prefix_raises(self, compiler: MongoQueryMethodCompiler):
        parsed = ParsedQuery(prefix="unknown_prefix")
        with pytest.raises(ValueError, match="Unknown prefix"):
            compiler.compile(parsed, object)


# ===========================================================================
# 5. Integration with QueryMethodParser
# ===========================================================================


class TestParserCompilerIntegration:
    """Test that parsed method names produce correct MongoDB filters."""

    def test_find_by_name_filter(self, parser: QueryMethodParser, compiler: MongoQueryMethodCompiler):
        parsed = parser.parse("find_by_name")
        filter_doc = compiler._build_filter(parsed, ["Alice"])
        assert filter_doc == {"name": "Alice"}

    def test_find_by_name_and_role_filter(self, parser: QueryMethodParser, compiler: MongoQueryMethodCompiler):
        parsed = parser.parse("find_by_name_and_role")
        filter_doc = compiler._build_filter(parsed, ["Alice", "admin"])
        assert filter_doc == {"$and": [{"name": "Alice"}, {"role": "admin"}]}

    def test_find_by_age_greater_than_filter(self, parser: QueryMethodParser, compiler: MongoQueryMethodCompiler):
        parsed = parser.parse("find_by_age_greater_than")
        filter_doc = compiler._build_filter(parsed, [18])
        assert filter_doc == {"age": {"$gt": 18}}

    def test_find_by_name_containing_filter(self, parser: QueryMethodParser, compiler: MongoQueryMethodCompiler):
        parsed = parser.parse("find_by_name_containing")
        filter_doc = compiler._build_filter(parsed, ["lic"])
        assert filter_doc["name"]["$regex"] == ".*lic.*"

    def test_find_by_with_order(self, parser: QueryMethodParser, compiler: MongoQueryMethodCompiler):
        parsed = parser.parse("find_by_active_order_by_name_asc")
        sort_spec = compiler._build_sort(parsed)
        assert sort_spec == [("name", pymongo.ASCENDING)]


# ===========================================================================
# 6. Structural protocol compliance
# ===========================================================================


class TestProtocolCompliance:
    """MongoQueryMethodCompiler structurally implements QueryMethodCompilerPort."""

    def test_has_compile_method(self):
        compiler = MongoQueryMethodCompiler()
        assert hasattr(compiler, "compile")
        assert callable(compiler.compile)

    def test_compile_signature(self):
        """Verify compile accepts (parsed, entity) and returns a callable."""
        compiler = MongoQueryMethodCompiler()
        parsed = ParsedQuery(prefix="find_by", predicates=[FieldPredicate("name", "eq")])
        result = compiler.compile(parsed, object)
        assert callable(result)
