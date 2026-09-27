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
"""Tests for @query decorator and QueryExecutor."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable
from pyfly.data.query import modifying
from pyfly.data.query_parser import InvalidQueryMethodError
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.query import QueryExecutor, query, transpile_jpql
from pyfly.data.relational.sqlalchemy.repository import Repository
from tests.support.backend_matrix import enable_sqlite_foreign_keys

# ---------------------------------------------------------------------------
# Test entity
# ---------------------------------------------------------------------------


class Item(BaseEntity):
    __tablename__ = "q_items"

    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(200), default=None)
    role: Mapped[str] = mapped_column(String(50), default="user")
    active: Mapped[bool] = mapped_column(default=True)
    score: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine(tmp_path: Path):
    """A SQLite file database with foreign keys on, holding every table of ``Base.metadata``."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    enable_sqlite_foreign_keys(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as sess:
        yield sess


@pytest.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    """Seed the database with known items."""
    items = [
        Item(name="Alice", email="alice@test.com", role="admin", active=True, score=90),
        Item(name="Bob", email="bob@test.com", role="user", active=True, score=70),
        Item(name="Carol", email="carol@test.com", role="admin", active=False, score=85),
        Item(name="Dave", email=None, role="user", active=False, score=60),
    ]
    session.add_all(items)
    await session.flush()
    return session


@pytest.fixture
def executor():
    return QueryExecutor()


# ===========================================================================
# Decorator Tests (unit, no DB)
# ===========================================================================


class TestQueryDecorator:
    """Test that @query stores metadata on functions."""

    def test_stores_query_metadata(self):
        """1. @query stores the SQL string as __pyfly_query__."""

        @query("SELECT * FROM items WHERE name = :name")
        async def find_by_name(self, name: str) -> list[Item]: ...

        assert find_by_name.__pyfly_query__ == "SELECT * FROM items WHERE name = :name"

    def test_native_true_stores_flag(self):
        """2. @query with native=True stores __pyfly_query_native__ = True."""

        @query("SELECT * FROM items WHERE name = :name", native=True)
        async def find_by_name(self, name: str) -> list[Item]: ...

        assert find_by_name.__pyfly_query_native__ is True

    def test_native_false_default(self):
        """3. @query with default native=False stores __pyfly_query_native__ = False."""

        @query("SELECT i FROM Item i WHERE i.name = :name")
        async def find_by_name(self, name: str) -> list[Item]: ...

        assert find_by_name.__pyfly_query_native__ is False

    def test_preserves_function_identity(self):
        """The decorator returns the original function (not a wrapper)."""

        @query("SELECT * FROM items")
        async def my_func(self) -> list[Item]: ...

        assert my_func.__name__ == "my_func"


class TestCompileQueryMethodValidation:
    """Input validation on compile_query_method."""

    def test_rejects_undecorated_method(self):
        """compile_query_method raises AttributeError for undecorated methods."""
        executor = QueryExecutor()

        async def plain_method(self) -> list[Item]: ...

        with pytest.raises(AttributeError, match="not decorated with @query"):
            executor.compile_query_method(plain_method, Item)


# ===========================================================================
# Transpiler Tests (unit, no DB)
# ===========================================================================


class QGroup(Base):
    """A table named with a reserved word, and an attribute whose column is named differently."""

    __tablename__ = "group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    label: Mapped[str] = mapped_column("title", String(40))
    active: Mapped[bool] = mapped_column(default=True)


ITEM_COLUMNS = (
    "i.name, i.email, i.role, i.active, i.score, i.id, i.created_at, i.updated_at, i.created_by, i.updated_by"
)


def _sql(jpql: str, entity: type = Item, dialect: Any = None, **kwargs: Any) -> str:
    return transpile_jpql(jpql, entity, dialect, **kwargs).sql


class TestJpqlTranspiler:
    """The JPQL transpiler rewrites tokens: aliases stay, attributes become columns, literals are untouched."""

    def test_an_entity_select_selects_its_columns_through_the_alias(self):
        assert _sql("SELECT i FROM Item i WHERE i.name = :name") == (
            f"SELECT {ITEM_COLUMNS} FROM q_items i WHERE i.name = :name"
        )

    def test_count_of_the_alias_counts_rows(self):
        assert _sql("SELECT COUNT(i) FROM Item i WHERE i.role = :role") == (
            "SELECT COUNT(*) FROM q_items i WHERE i.role = :role"
        )
        assert _sql("SELECT COUNT(DISTINCT i) FROM Item AS i") == "SELECT COUNT(DISTINCT i.id) FROM q_items AS i"

    def test_a_selected_attribute_keeps_its_alias(self):
        assert _sql("SELECT c.name FROM Item c WHERE c.active = true") == (
            "SELECT c.name FROM q_items c WHERE c.active = true"
        )

    def test_attributes_name_their_columns(self):
        assert _sql("SELECT g.label FROM QGroup g WHERE g.label = :label", QGroup) == (
            "SELECT g.title FROM group g WHERE g.title = :label"
        )

    def test_names_are_quoted_for_the_dialect(self):
        sql = _sql("SELECT g FROM QGroup g WHERE g.label = :label", QGroup, postgresql.dialect())
        assert sql == 'SELECT g.id, g.title, g.active FROM "group" g WHERE g.title = :label'

    def test_boolean_literals_are_what_the_dialect_accepts(self):
        jpql = "SELECT i FROM Item i WHERE i.active = true AND i.score != false"
        assert _sql(jpql, dialect=postgresql.dialect()).endswith("WHERE i.active = true AND i.score != false")
        assert _sql(jpql, dialect=sqlite.dialect()).endswith("WHERE i.active = 1 AND i.score != 0")
        assert _sql(jpql, dialect=mysql.dialect()).endswith("WHERE i.active = 1 AND i.score != 0")

    def test_is_true_and_string_literals_are_left_as_written(self):
        jpql = "SELECT c.name FROM Item c WHERE c.active IS true OR c.active IS NOT false OR c.role = 'status = true'"
        assert _sql(jpql, dialect=sqlite.dialect()) == (
            "SELECT c.name FROM q_items c WHERE c.active IS true OR c.active IS NOT false OR c.role = 'status = true'"
        )

    def test_the_alias_is_rewritten_only_where_it_is_one(self):
        """C049/C051: the regex stripped 'i.' from literals, from identifiers ending in the alias, and from
        schema-qualified names."""
        jpql = (
            "SELECT i.name FROM Item i WHERE i.email LIKE '%@example.com' AND i.role = 'wiki.pyfly.io' "
            "AND i.name IN (SELECT r.code FROM sales_data.regions r) AND i.email <> 'n/a.'"
        )
        assert _sql(jpql) == (
            "SELECT i.name FROM q_items i WHERE i.email LIKE '%@example.com' AND i.role = 'wiki.pyfly.io' "
            "AND i.name IN (SELECT r.code FROM sales_data.regions r) AND i.email <> 'n/a.'"
        )

    def test_a_correlated_subquery_keeps_its_correlation(self):
        jpql = "SELECT a FROM Item a WHERE EXISTS (SELECT 1 FROM q_orders o WHERE o.item_id = a.id)"
        assert _sql(jpql).endswith("FROM q_items a WHERE EXISTS (SELECT 1 FROM q_orders o WHERE o.item_id = a.id)")

    def test_positional_parameters_name_the_methods_parameters(self):
        assert _sql("SELECT i.id FROM Item i WHERE i.role = ?1 AND i.score > ?2", positional=["role", "floor"]) == (
            "SELECT i.id FROM q_items i WHERE i.role = :role AND i.score > :floor"
        )
        with pytest.raises(InvalidQueryMethodError, match=r"\?3 names parameter 3"):
            _sql("SELECT i.id FROM Item i WHERE i.role = ?3", positional=["role"])

    def test_in_binds_a_collection(self):
        transpiled = transpile_jpql("SELECT i.id FROM Item i WHERE i.id IN (:ids) OR i.role IN :roles", Item)
        assert transpiled.sql == "SELECT i.id FROM q_items i WHERE i.id IN :ids OR i.role IN :roles"
        assert transpiled.expanding == {"ids", "roles"}

    def test_colons_in_literals_are_not_parameters(self):
        transpiled = transpile_jpql("SELECT i.id FROM Item i WHERE i.name = 'a:b' AND i.email = :email", Item)
        assert transpiled.binds == ("email",)
        assert "'a\\:b'" in transpiled.sql

    def test_update_and_delete_drop_the_alias(self):
        update = "UPDATE Item i SET i.active = false, i.score = i.score + 1 WHERE i.role = :role"
        assert _sql(update, dialect=postgresql.dialect()) == (
            "UPDATE q_items SET active = false, score = q_items.score + 1 WHERE q_items.role = :role"
        )
        delete = "DELETE FROM QGroup AS g WHERE g.label = :label"
        assert _sql(delete, QGroup, postgresql.dialect()) == 'DELETE FROM "group" WHERE "group".title = :label'

    def test_an_unknown_attribute_or_an_unterminated_literal_fails(self):
        with pytest.raises(InvalidQueryMethodError, match="no attribute or column 'nmae'"):
            _sql("SELECT i FROM Item i WHERE i.nmae = :name")
        with pytest.raises(InvalidQueryMethodError, match="Unterminated"):
            _sql("SELECT i FROM Item i WHERE i.name = 'x")

    def test_the_legacy_static_method_still_transpiles(self):
        assert QueryExecutor._transpile_jpql("SELECT i FROM Item i", Item) == f"SELECT {ITEM_COLUMNS} FROM q_items i"


class _Repo(Repository[Item, UUID]):
    """Methods for the startup checks (each test compiles one)."""

    @query("SELECT i FROM Item i WHERE i.name = :nme")
    async def unknown_parameter(self, name: str) -> list[Item]: ...

    @query("UPDATE Item i SET i.active = false")
    async def update_without_modifying(self) -> int: ...

    @modifying
    @query("SELECT i FROM Item i")
    async def modifying_select(self) -> list[Item]: ...

    @modifying
    @query("UPDATE Item i SET i.active = false")
    async def modifying_returning_entities(self) -> list[Item]: ...

    @query("SELECT i FROM Item i")
    async def paged(self) -> Page[Item]: ...

    @query("SELECT i FROM Item i ORDER BY i.name")
    async def with_pageable(self, pageable: Pageable) -> list[Item]: ...

    @query("SELECT i FROM Item i")
    async def nothing(self) -> None: ...


class TestQueryMethodsAreCheckedAtStartup:
    @pytest.mark.parametrize(
        ("method", "message"),
        [
            ("unknown_parameter", ":nme"),
            ("update_without_modifying", "mark the method @modifying"),
            ("modifying_select", "this one is a SELECT"),
            ("modifying_returning_entities", "returns int"),
            ("paged", "not a Page or Slice"),
            ("with_pageable", "takes no Pageable or Sort"),
            ("nothing", "not None"),
        ],
    )
    def test_the_method_fails_to_compile(self, method: str, message: str):
        with pytest.raises(InvalidQueryMethodError, match=message):
            QueryExecutor().compile_query_method(getattr(_Repo, method), Item)


# ===========================================================================
# Executor Tests (with DB)
# ===========================================================================


class TestQueryExecutorNative:
    """Test QueryExecutor with native SQL queries."""

    @pytest.mark.asyncio
    async def test_native_sql_returns_results(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """8. Native SQL query returns mapped entity results."""

        @query("SELECT * FROM q_items WHERE role = :role", native=True)
        async def find_by_role(self, role: str) -> list[Item]: ...

        compiled = executor.compile_query_method(find_by_role, Item)
        results = await compiled(seeded_session, role="admin")
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_native_count_returns_int(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """10. Count query returns int."""

        @query("SELECT COUNT(*) FROM q_items WHERE active = 1", native=True)
        async def count_active(self) -> int: ...

        compiled = executor.compile_query_method(count_active, Item)
        result = await compiled(seeded_session)
        assert result == 2
        assert isinstance(result, int)


class TestQueryExecutorJpql:
    """Test QueryExecutor with JPQL-like queries."""

    @pytest.mark.asyncio
    async def test_jpql_returns_results(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """9. JPQL query returns mapped entity results."""

        @query("SELECT i FROM Item i WHERE i.role = :role")
        async def find_by_role(self, role: str) -> list[Item]: ...

        compiled = executor.compile_query_method(find_by_role, Item)
        results = await compiled(seeded_session, role="admin")
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_jpql_count_returns_int(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """JPQL COUNT query returns integer."""

        @query("SELECT COUNT(i) FROM Item i WHERE i.active = true")
        async def count_active(self) -> int: ...

        compiled = executor.compile_query_method(count_active, Item)
        result = await compiled(seeded_session)
        assert result == 2
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_multiple_parameters(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """11. Query with multiple parameters binds all correctly."""

        @query("SELECT i FROM Item i WHERE i.role = :role AND i.active = :active")
        async def find_by_role_and_active(self, role: str, active: bool) -> list[Item]: ...

        compiled = executor.compile_query_method(find_by_role_and_active, Item)
        results = await compiled(seeded_session, role="admin", active=True)
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_list(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """Query with no matches returns empty list."""

        @query("SELECT i FROM Item i WHERE i.name = :name")
        async def find_by_name(self, name: str) -> list[Item]: ...

        compiled = executor.compile_query_method(find_by_name, Item)
        results = await compiled(seeded_session, name="Nonexistent")
        assert results == []

    @pytest.mark.asyncio
    async def test_native_with_like(self, executor: QueryExecutor, seeded_session: AsyncSession):
        """Native SQL with LIKE pattern works."""

        @query("SELECT * FROM q_items WHERE email LIKE :pattern", native=True)
        async def find_by_email_pattern(self, pattern: str) -> list[Item]: ...

        compiled = executor.compile_query_method(find_by_email_pattern, Item)
        results = await compiled(seeded_session, pattern="%@test.com")
        assert len(results) == 3  # Alice, Bob, Carol (Dave has no email)
