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

import pytest
from sqlalchemy import Integer, String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.query import QueryExecutor, query

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
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
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


class TestJpqlTranspiler:
    """Test the JPQL-to-SQL transpiler."""

    def test_simple_select_transpiles(self):
        """4. Simple JPQL select transpiles to SQL."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT i FROM Item i WHERE i.name = :name",
            Item,
        )
        assert "SELECT * FROM q_items" in sql
        assert "name = :name" in sql
        # Alias should be stripped
        assert "i." not in sql

    def test_count_transpiles(self):
        """5. COUNT JPQL transpiles correctly."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT COUNT(i) FROM Item i WHERE i.role = :role",
            Item,
        )
        assert "SELECT COUNT(*) FROM q_items" in sql
        assert "role = :role" in sql

    def test_alias_stripping(self):
        """6. All alias references are stripped from the query."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT u FROM Item u WHERE u.email = :email AND u.role = :role",
            Item,
        )
        assert "u." not in sql
        assert "email = :email" in sql
        assert "role = :role" in sql

    def test_true_false_replacement(self):
        """7. true/false are replaced with 1/0."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT i FROM Item i WHERE i.active = true",
            Item,
        )
        assert "= 1" in sql
        assert "true" not in sql.lower().replace("q_items", "")

    def test_false_replacement(self):
        """false is replaced with 0."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT i FROM Item i WHERE i.active = false",
            Item,
        )
        assert "= 0" in sql

    def test_uses_tablename(self):
        """Transpiler uses __tablename__ from entity."""
        sql = QueryExecutor._transpile_jpql(
            "SELECT i FROM Item i WHERE i.name = :name",
            Item,
        )
        assert "q_items" in sql

    def test_regex_does_not_match_into_where(self):
        """Regression: FROM regex must not eat SQL keywords as alias."""
        # If the entity name does not appear, transpiler should
        # degrade gracefully (no alias found, minimal transformation).
        sql = QueryExecutor._transpile_jpql(
            "SELECT i FROM Item i WHERE i.score > :min",
            Item,
        )
        # WHERE must survive intact
        assert "WHERE" in sql
        assert "score > :min" in sql


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
