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
"""Tests for derived query method parser and compiler."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional, Protocol

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.projection import projection
from pyfly.data.query_parser import (
    ElementKind,
    FieldPredicate,
    InvalidQueryMethodError,
    ParsedQuery,
    QueryMethodParser,
    ResultKind,
    ResultShape,
    is_special_parameter,
    result_shape,
)
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.query_compiler import QueryMethodCompiler
from tests.support.backend_matrix import enable_sqlite_foreign_keys

# ---------------------------------------------------------------------------
# Test entity for compiler tests
# ---------------------------------------------------------------------------


class Product(BaseEntity):
    __tablename__ = "qp_products"

    name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="active")
    role: Mapped[str] = mapped_column(String(50), default="basic")
    email: Mapped[str | None] = mapped_column(String(200), default=None)
    age: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(default=0.0)
    active: Mapped[bool] = mapped_column(default=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine(tmp_path: Path):
    """A SQLite file database with foreign keys on, holding every table of ``Base.metadata``."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    enable_sqlite_foreign_keys(engine)
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
async def seeded_session(session: AsyncSession) -> AsyncSession:
    """Seed the database with a known set of products."""
    products = [
        Product(name="Alpha", status="active", role="admin", email="alpha@test.com", age=25, price=10.0, active=True),
        Product(name="Beta", status="active", role="user", email="beta@test.com", age=30, price=20.0, active=True),
        Product(name="Gamma", status="inactive", role="admin", email=None, age=35, price=30.0, active=False),
        Product(name="Delta", status="inactive", role="user", email="delta@test.com", age=40, price=40.0, active=False),
    ]
    session.add_all(products)
    await session.flush()
    return session


@pytest.fixture
def parser():
    return QueryMethodParser()


@pytest.fixture
def compiler():
    return QueryMethodCompiler()


# ===========================================================================
# Parser Tests (unit, no DB)
# ===========================================================================


class TestParserBasicPrefixes:
    """Test parsing of basic method name prefixes."""

    def test_find_by_email(self, parser: QueryMethodParser):
        """1. find_by_email -> prefix=find_by, field=email, op=eq."""
        result = parser.parse("find_by_email")
        assert result.prefix == "find_by"
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "email"
        assert result.predicates[0].operator == "eq"
        assert result.connectors == []

    def test_count_by_active(self, parser: QueryMethodParser):
        """7. count_by_active -> prefix=count_by."""
        result = parser.parse("count_by_active")
        assert result.prefix == "count_by"
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "active"
        assert result.predicates[0].operator == "eq"

    def test_exists_by_email(self, parser: QueryMethodParser):
        """8. exists_by_email -> prefix=exists_by."""
        result = parser.parse("exists_by_email")
        assert result.prefix == "exists_by"
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "email"

    def test_delete_by_status(self, parser: QueryMethodParser):
        """9. delete_by_status -> prefix=delete_by."""
        result = parser.parse("delete_by_status")
        assert result.prefix == "delete_by"
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "status"

    def test_invalid_prefix_raises(self, parser: QueryMethodParser):
        """14. Invalid prefix raises ValueError."""
        with pytest.raises(ValueError, match="must start with one of"):
            parser.parse("get_by_name")


class TestParserConnectors:
    """Test parsing of AND/OR connectors."""

    def test_find_by_status_and_role(self, parser: QueryMethodParser):
        """2. find_by_status_and_role -> two predicates with AND."""
        result = parser.parse("find_by_status_and_role")
        assert result.prefix == "find_by"
        assert len(result.predicates) == 2
        assert result.predicates[0].field_name == "status"
        assert result.predicates[0].operator == "eq"
        assert result.predicates[1].field_name == "role"
        assert result.predicates[1].operator == "eq"
        assert result.connectors == ["and"]

    def test_find_by_status_or_role(self, parser: QueryMethodParser):
        """5. find_by_status_or_role -> two predicates with OR."""
        result = parser.parse("find_by_status_or_role")
        assert result.prefix == "find_by"
        assert len(result.predicates) == 2
        assert result.predicates[0].field_name == "status"
        assert result.predicates[1].field_name == "role"
        assert result.connectors == ["or"]


class TestParserOperators:
    """Test parsing of comparison operators."""

    def test_find_by_age_greater_than(self, parser: QueryMethodParser):
        """3. find_by_age_greater_than -> field=age, op=gt."""
        result = parser.parse("find_by_age_greater_than")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "gt"

    def test_find_by_name_like(self, parser: QueryMethodParser):
        """4. find_by_name_like -> field=name, op=like."""
        result = parser.parse("find_by_name_like")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "name"
        assert result.predicates[0].operator == "like"

    def test_find_by_age_between(self, parser: QueryMethodParser):
        """10. find_by_age_between -> field=age, op=between."""
        result = parser.parse("find_by_age_between")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "between"

    def test_find_by_email_is_null(self, parser: QueryMethodParser):
        """11. find_by_email_is_null -> field=email, op=is_null."""
        result = parser.parse("find_by_email_is_null")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "email"
        assert result.predicates[0].operator == "is_null"

    def test_find_by_name_containing(self, parser: QueryMethodParser):
        """12. find_by_name_containing -> field=name, op=containing."""
        result = parser.parse("find_by_name_containing")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "name"
        assert result.predicates[0].operator == "containing"

    def test_find_by_role_in(self, parser: QueryMethodParser):
        """13. find_by_role_in -> field=role, op=in."""
        result = parser.parse("find_by_role_in")
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "role"
        assert result.predicates[0].operator == "in"


class TestParserOrderBy:
    """Test parsing of order by clauses."""

    def test_find_by_name_order_by_created_at_desc(self, parser: QueryMethodParser):
        """6. find_by_name_order_by_created_at_desc -> predicate + order."""
        result = parser.parse("find_by_name_order_by_created_at_desc")
        assert result.prefix == "find_by"
        assert len(result.predicates) == 1
        assert result.predicates[0].field_name == "name"
        assert result.predicates[0].operator == "eq"
        assert len(result.order_clauses) == 1
        assert result.order_clauses[0].field_name == "created_at"
        assert result.order_clauses[0].direction == "desc"

    def test_order_by_multiple_fields(self, parser: QueryMethodParser):
        """Order by multiple fields: name_asc_price_desc."""
        result = parser.parse("find_by_status_order_by_name_asc_price_desc")
        assert len(result.order_clauses) == 2
        assert result.order_clauses[0].field_name == "name"
        assert result.order_clauses[0].direction == "asc"
        assert result.order_clauses[1].field_name == "price"
        assert result.order_clauses[1].direction == "desc"

    def test_order_by_default_asc(self, parser: QueryMethodParser):
        """Order by without explicit direction defaults to asc."""
        result = parser.parse("find_by_status_order_by_name")
        assert len(result.order_clauses) == 1
        assert result.order_clauses[0].field_name == "name"
        assert result.order_clauses[0].direction == "asc"


class TestParserEdgeCases:
    """Test edge cases and complex combinations."""

    def test_greater_than_equal(self, parser: QueryMethodParser):
        """Ensure _greater_than_equal does not get confused with _greater_than."""
        result = parser.parse("find_by_age_greater_than_equal")
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "gte"

    def test_less_than_equal(self, parser: QueryMethodParser):
        """Ensure _less_than_equal parses correctly."""
        result = parser.parse("find_by_age_less_than_equal")
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "lte"

    def test_less_than(self, parser: QueryMethodParser):
        """Ensure _less_than parses correctly."""
        result = parser.parse("find_by_age_less_than")
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "lt"

    def test_is_not_null(self, parser: QueryMethodParser):
        """Ensure _is_not_null does not get confused with _not or _is_null."""
        result = parser.parse("find_by_email_is_not_null")
        assert result.predicates[0].field_name == "email"
        assert result.predicates[0].operator == "is_not_null"

    def test_not_operator(self, parser: QueryMethodParser):
        """Ensure _not parses as != operator."""
        result = parser.parse("find_by_status_not")
        assert result.predicates[0].field_name == "status"
        assert result.predicates[0].operator == "not"

    def test_operator_with_and_connector(self, parser: QueryMethodParser):
        """Operators combined with connectors."""
        result = parser.parse("find_by_age_greater_than_and_status")
        assert len(result.predicates) == 2
        assert result.predicates[0].field_name == "age"
        assert result.predicates[0].operator == "gt"
        assert result.predicates[1].field_name == "status"
        assert result.predicates[1].operator == "eq"
        assert result.connectors == ["and"]


class TestParserPrecedence:
    """``and`` binds tighter than ``or`` (Spring's PartTree), whether or not the parse knows the properties."""

    @pytest.mark.parametrize("properties", [None, {"owner", "tag", "balance"}])
    def test_or_splits_before_and(self, parser: QueryMethodParser, properties: set[str] | None):
        result = parser.parse("find_by_owner_or_tag_and_balance", properties=properties)
        assert [[p.field_name for p in group] for group in result.groups] == [["owner"], ["tag", "balance"]]

    @pytest.mark.parametrize("properties", [None, {"owner", "tag", "balance"}])
    def test_and_groups_on_both_sides_of_or(self, parser: QueryMethodParser, properties: set[str] | None):
        result = parser.parse("find_by_owner_and_tag_or_owner_and_balance", properties=properties)
        assert [[p.field_name for p in group] for group in result.groups] == [["owner", "tag"], ["owner", "balance"]]

    def test_groups_of_a_hand_built_parsed_query(self):
        parsed = ParsedQuery(
            prefix="find_by",
            predicates=[FieldPredicate("a"), FieldPredicate("b"), FieldPredicate("c"), FieldPredicate("d")],
            connectors=["and", "or", "and"],
        )
        assert [[p.field_name for p in group] for group in parsed.groups] == [["a", "b"], ["c", "d"]]
        assert ParsedQuery(prefix="find_by").groups == []

    def test_argument_count_follows_the_operators(self, parser: QueryMethodParser):
        result = parser.parse("find_by_age_between_and_email_is_null_and_role_in")
        assert result.argument_count == 3


PROPERTIES = frozenset(
    {
        "name",
        "status",
        "role",
        "email",
        "age",
        "active",
        "logged_in",
        "opt_in",
        "terms_and_conditions_accepted",
        "order",
        "position",
        "created_at",
    }
)


class TestParserAgainstProperties:
    """With the entity's properties, the names decide how the method name splits, and typos fail at once."""

    def test_a_property_ending_in_an_operator_word_is_read_whole(self, parser: QueryMethodParser):
        for name in ("logged_in", "opt_in"):
            result = parser.parse(f"find_by_{name}", properties=PROPERTIES)
            assert [(p.field_name, p.operator) for p in result.predicates] == [(name, "eq")]

    def test_a_property_holding_a_connector_is_read_whole(self, parser: QueryMethodParser):
        result = parser.parse("find_by_terms_and_conditions_accepted", properties=PROPERTIES)
        assert [(p.field_name, p.operator) for p in result.predicates] == [("terms_and_conditions_accepted", "eq")]
        assert result.connectors == []

    def test_an_operator_after_such_a_property(self, parser: QueryMethodParser):
        result = parser.parse("find_by_logged_in_in_and_opt_in_not", properties=PROPERTIES)
        assert [(p.field_name, p.operator) for p in result.predicates] == [("logged_in", "in"), ("opt_in", "not")]

    def test_an_unknown_property_fails_naming_it(self, parser: QueryMethodParser):
        with pytest.raises(InvalidQueryMethodError, match="'nmae' names no property"):
            parser.parse("find_by_nmae", properties=PROPERTIES)

    def test_an_unknown_order_property_fails(self, parser: QueryMethodParser):
        with pytest.raises(InvalidQueryMethodError, match="order_by_nmae"):
            parser.parse("find_by_name_order_by_nmae", properties=PROPERTIES)

    def test_a_property_named_order_before_order_by(self, parser: QueryMethodParser):
        result = parser.parse("find_by_order_order_by_position_desc", properties=PROPERTIES)
        assert [(p.field_name, p.operator) for p in result.predicates] == [("order", "eq")]
        assert [(o.field_name, o.direction) for o in result.order_clauses] == [("position", "desc")]

    def test_order_by_without_criteria(self, parser: QueryMethodParser):
        result = parser.parse("find_by_order_by_created_at_desc_name", properties=PROPERTIES)
        assert result.predicates == []
        assert [(o.field_name, o.direction) for o in result.order_clauses] == [("created_at", "desc"), ("name", "asc")]

    @pytest.mark.parametrize(
        ("suffix", "operator"),
        [
            ("", "eq"),
            ("_is", "eq"),
            ("_equals", "eq"),
            ("_is_not", "not"),
            ("_not", "not"),
            ("_after", "gt"),
            ("_is_before", "lt"),
            ("_greater_than_equal", "gte"),
            ("_is_less_than", "lt"),
            ("_is_between", "between"),
            ("_null", "is_null"),
            ("_is_null", "is_null"),
            ("_is_not_null", "is_not_null"),
            ("_not_null", "is_not_null"),
            ("_like", "like"),
            ("_not_like", "not_like"),
            ("_starting_with", "starting_with"),
            ("_starts_with", "starting_with"),
            ("_ending_with", "ending_with"),
            ("_is_containing", "containing"),
            ("_contains", "containing"),
            ("_not_containing", "not_containing"),
            ("_in", "in"),
            ("_is_not_in", "not_in"),
        ],
    )
    def test_keywords(self, parser: QueryMethodParser, suffix: str, operator: str):
        result = parser.parse(f"find_by_name{suffix}", properties=PROPERTIES)
        assert [(p.field_name, p.operator) for p in result.predicates] == [("name", operator)]

    def test_boolean_keywords_take_no_argument(self, parser: QueryMethodParser):
        result = parser.parse("count_by_active_true_or_active_is_false", properties=PROPERTIES)
        assert [(p.field_name, p.operator) for p in result.predicates] == [
            ("active", "is_true"),
            ("active", "is_false"),
        ]
        assert result.argument_count == 0

    def test_ignore_case(self, parser: QueryMethodParser):
        result = parser.parse("find_by_name_starting_with_ignore_case_and_email_ignoring_case", properties=PROPERTIES)
        assert [(p.field_name, p.operator, p.ignore_case) for p in result.predicates] == [
            ("name", "starting_with", True),
            ("email", "eq", True),
        ]
        assert result.all_ignore_case is False

    def test_all_ignore_case(self, parser: QueryMethodParser):
        result = parser.parse("find_by_name_and_email_all_ignore_case_order_by_name", properties=PROPERTIES)
        assert [p.field_name for p in result.predicates] == ["name", "email"]
        assert result.all_ignore_case is True
        assert [o.field_name for o in result.order_clauses] == ["name"]

    def test_without_properties_the_original_keywords_apply(self, parser: QueryMethodParser):
        result = parser.parse("find_by_name_starting_with")
        assert [(p.field_name, p.operator) for p in result.predicates] == [("name_starting_with", "eq")]
        result = parser.parse("find_by_logged_in")
        assert [(p.field_name, p.operator) for p in result.predicates] == [("logged", "in")]


class TestResultShape:
    """Return annotations become result shapes."""

    def test_containers(self):
        assert result_shape(list[Product], Product) == ResultShape(ResultKind.LIST, ElementKind.ENTITY, Product)
        assert result_shape(Sequence[Product], Product).kind is ResultKind.LIST
        assert result_shape(Page[Product], Product) == ResultShape(ResultKind.PAGE, ElementKind.ENTITY, Product)
        assert result_shape(Slice[Product], Product).kind is ResultKind.SLICE
        assert result_shape(list, Product) == ResultShape(ResultKind.LIST, ElementKind.ENTITY, Product)

    def test_single_results(self):
        assert result_shape(Product | None, Product) == ResultShape(ResultKind.ONE, ElementKind.ENTITY, Product)
        assert result_shape(Optional[Product], Product).kind is ResultKind.ONE  # noqa: UP045
        assert result_shape(Product, Product).kind is ResultKind.ONE
        assert result_shape(None, Product) == ResultShape(ResultKind.NONE, ElementKind.ENTITY, None)

    def test_elements(self):
        assert result_shape(int, Product) == ResultShape(ResultKind.ONE, ElementKind.SCALAR, int)
        assert result_shape(list[str], Product) == ResultShape(ResultKind.LIST, ElementKind.SCALAR, str)
        assert result_shape(list[tuple[int, str]], Product).element is ElementKind.ROW
        assert result_shape(list[dict[str, Any]], Product).element is ElementKind.MAPPING
        assert result_shape(list[ProductView], Product) == ResultShape(
            ResultKind.LIST, ElementKind.PROJECTION, ProductView
        )
        assert result_shape(list[ProductRow], Product) == ResultShape(ResultKind.LIST, ElementKind.OBJECT, ProductRow)
        assert result_shape(list[Any], Product).element is ElementKind.ENTITY

    def test_a_union_of_two_types_is_refused(self):
        with pytest.raises(InvalidQueryMethodError):
            result_shape(int | str, Product)


class TestSpecialParameters:
    def test_pageable_and_sort_are_special(self):
        assert is_special_parameter(Pageable)
        assert is_special_parameter(Sort | None)
        assert not is_special_parameter(str)
        assert not is_special_parameter(int | None)


@projection
class ProductView(Protocol):
    name: str


@dataclasses.dataclass
class ProductRow:
    name: str


# ===========================================================================
# Compiler Tests (with DB)
# ===========================================================================


class TestCompilerFind:
    """Test compiled find queries against a real database."""

    @pytest.mark.asyncio
    async def test_find_by_name(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """15. find_by_name -> returns matching entities."""
        parsed = parser.parse("find_by_name")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, "Alpha")
        assert len(results) == 1
        assert results[0].name == "Alpha"

    @pytest.mark.asyncio
    async def test_find_by_status_and_role(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """16. find_by_status_and_role -> AND combination."""
        parsed = parser.parse("find_by_status_and_role")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, "active", "admin")
        assert len(results) == 1
        assert results[0].name == "Alpha"

    @pytest.mark.asyncio
    async def test_find_by_age_greater_than(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """17. find_by_age_greater_than -> comparison."""
        parsed = parser.parse("find_by_age_greater_than")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, 30)
        names = sorted(r.name for r in results)
        assert names == ["Delta", "Gamma"]

    @pytest.mark.asyncio
    async def test_find_by_name_order_by_price_desc(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """20. find_by_name_order_by_price_desc -> sorted results."""
        # Use a predicate that matches multiple to test ordering
        parsed = parser.parse("find_by_active_order_by_price_desc")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, True)
        assert len(results) == 2
        assert results[0].name == "Beta"
        assert results[1].name == "Alpha"

    @pytest.mark.asyncio
    async def test_find_by_status_or_role(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_status_or_role with OR connector."""
        parsed = parser.parse("find_by_status_or_role")
        query_fn = compiler.compile(parsed, Product)
        # status=inactive OR role=admin -> Gamma(inactive,admin), Delta(inactive,user), Alpha(active,admin)
        results = await query_fn(seeded_session, "inactive", "admin")
        names = sorted(r.name for r in results)
        assert names == ["Alpha", "Delta", "Gamma"]

    @pytest.mark.asyncio
    async def test_find_by_email_is_null(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_email_is_null -> no-arg predicate."""
        parsed = parser.parse("find_by_email_is_null")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session)
        assert len(results) == 1
        assert results[0].name == "Gamma"

    @pytest.mark.asyncio
    async def test_find_by_email_is_not_null(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_email_is_not_null -> no-arg predicate."""
        parsed = parser.parse("find_by_email_is_not_null")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session)
        names = sorted(r.name for r in results)
        assert names == ["Alpha", "Beta", "Delta"]

    @pytest.mark.asyncio
    async def test_find_by_name_containing(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_name_containing -> LIKE %value%."""
        parsed = parser.parse("find_by_name_containing")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, "lph")
        assert len(results) == 1
        assert results[0].name == "Alpha"

    @pytest.mark.asyncio
    async def test_find_by_name_like(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_name_like -> LIKE with user-provided pattern."""
        parsed = parser.parse("find_by_name_like")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, "A%")
        assert len(results) == 1
        assert results[0].name == "Alpha"

    @pytest.mark.asyncio
    async def test_find_by_role_in(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_role_in -> IN clause."""
        parsed = parser.parse("find_by_role_in")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, ["admin"])
        names = sorted(r.name for r in results)
        assert names == ["Alpha", "Gamma"]

    @pytest.mark.asyncio
    async def test_find_by_age_between(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """find_by_age_between -> BETWEEN with two args."""
        parsed = parser.parse("find_by_age_between")
        query_fn = compiler.compile(parsed, Product)
        results = await query_fn(seeded_session, 28, 38)
        names = sorted(r.name for r in results)
        assert names == ["Beta", "Gamma"]


class TestCompilerCount:
    """Test compiled count queries."""

    @pytest.mark.asyncio
    async def test_count_by_active(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """18. count_by_active -> returns integer count."""
        parsed = parser.parse("count_by_active")
        query_fn = compiler.compile(parsed, Product)
        count = await query_fn(seeded_session, True)
        assert count == 2
        assert isinstance(count, int)


class TestCompilerExists:
    """Test compiled exists queries."""

    @pytest.mark.asyncio
    async def test_exists_by_email(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """19. exists_by_email -> returns bool."""
        parsed = parser.parse("exists_by_email")
        query_fn = compiler.compile(parsed, Product)

        result = await query_fn(seeded_session, "alpha@test.com")
        assert result is True

        result = await query_fn(seeded_session, "nonexistent@test.com")
        assert result is False


class TestCompilerDelete:
    """Test compiled delete queries."""

    @pytest.mark.asyncio
    async def test_delete_by_status(
        self, parser: QueryMethodParser, compiler: QueryMethodCompiler, seeded_session: AsyncSession
    ):
        """delete_by_status -> deletes matching entities and returns count."""
        parsed = parser.parse("delete_by_status")
        query_fn = compiler.compile(parsed, Product)
        deleted_count = await query_fn(seeded_session, "inactive")
        assert deleted_count == 2

        # Verify remaining entities
        from sqlalchemy import select

        result = await seeded_session.execute(select(Product))
        remaining = list(result.scalars().all())
        assert len(remaining) == 2
        names = sorted(r.name for r in remaining)
        assert names == ["Alpha", "Beta"]
