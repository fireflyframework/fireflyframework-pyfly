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
from sqlalchemy import Integer, String, text
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable
from pyfly.data.query import modifying
from pyfly.data.query_parser import InvalidQueryMethodError
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.query import QueryExecutor, _native, query, tokenize, transpile_jpql
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


class QNode(Base):
    """A tree: a node's parent is another node of the same table."""

    __tablename__ = "q_node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    children: Mapped[int] = mapped_column(Integer, default=0)
    leaf: Mapped[bool] = mapped_column(default=False)


ITEM_COLUMNS = (
    "i.name, i.email, i.role, i.active, i.score, i.id, i.created_at, i.updated_at, i.created_by, i.updated_by"
)


def _sql(jpql: str, entity: type = Item, dialect: Any = None, **kwargs: Any) -> str:
    return transpile_jpql(jpql, entity, dialect, **kwargs).sql


def _rendered(sql: str) -> str:
    """*sql* as ``text()`` sends it to PostgreSQL (its ``\\:`` escapes resolved)."""
    return str(text(sql).compile(dialect=postgresql.dialect()))


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

    @pytest.mark.parametrize("literal", ["'10:30'", "'10\\:30'", "'a :b'", "'a \\:b'"])
    def test_a_colon_escaped_or_not_reaches_the_database_as_a_colon(self, literal: str):
        """``\\:`` was the documented way to keep a colon out of the parameters of ``text()``, so queries written
        before carry it: it is escaped once, never twice (a doubled escape left a backslash in the literal)."""
        expected = literal.replace("\\", "")
        jpql = transpile_jpql(f"SELECT i.id FROM Item i WHERE i.name = {literal}", Item)
        assert _rendered(jpql.sql) == f"SELECT i.id FROM q_items i WHERE i.name = {expected}"

    def test_colons_in_comments_and_quoted_names_are_not_parameters(self):
        transpiled = transpile_jpql(
            'SELECT i.id AS "id:label" FROM Item i -- :unused is not a parameter\nWHERE i.role = :role', Item
        )
        assert transpiled.binds == ("role",)
        rendered = text(transpiled.sql).compile(dialect=postgresql.dialect())
        assert set(rendered.params) == {"role"}
        assert '"id:label"' in str(rendered) and "-- :unused is not a parameter" in str(rendered)

    def test_update_and_delete_drop_the_alias(self):
        update = "UPDATE Item i SET i.active = false, i.score = i.score + 1 WHERE i.role = :role"
        assert _sql(update, dialect=postgresql.dialect()) == (
            "UPDATE q_items SET active = false, score = q_items.score + 1 WHERE q_items.role = :role"
        )
        delete = "DELETE FROM QGroup AS g WHERE g.label = :label"
        assert _sql(delete, QGroup, postgresql.dialect()) == 'DELETE FROM "group" WHERE "group".title = :label'

    def test_update_and_delete_keep_the_alias_of_a_subquery_over_their_own_entity(self):
        """Only the target's alias is dropped: a subquery over the same entity keeps its own, so the subquery stays
        correlated with the row being changed (dropping every alias made ``c.parent_id = n.id`` compare a row with
        itself, and changed every row that has a child instead of the leaves)."""
        dialect = postgresql.dialect()
        delete = "DELETE FROM QNode n WHERE NOT EXISTS (SELECT 1 FROM QNode c WHERE c.parent_id = n.id)"
        assert _sql(delete, QNode, dialect) == (
            "DELETE FROM q_node WHERE NOT EXISTS (SELECT 1 FROM q_node c WHERE c.parent_id = q_node.id)"
        )
        update = (
            "UPDATE QNode AS n SET n.leaf = true WHERE NOT EXISTS (SELECT 1 FROM QNode AS c WHERE c.parent_id = n.id)"
        )
        assert _sql(update, QNode, dialect) == (
            "UPDATE q_node SET leaf = true WHERE NOT EXISTS (SELECT 1 FROM q_node AS c WHERE c.parent_id = q_node.id)"
        )
        counted = "UPDATE QNode n SET n.children = (SELECT COUNT(c) FROM QNode c WHERE c.parent_id = n.id)"
        assert _sql(counted, QNode, dialect) == (
            "UPDATE q_node SET children = (SELECT COUNT(*) FROM q_node c WHERE c.parent_id = q_node.id)"
        )

    def test_an_alias_a_subquery_declares_again_names_the_subquerys_entity_inside_it(self):
        """``n`` inside the subquery is the subquery's ``n``, as SQL scopes it; outside, the target's."""
        shadowed = "DELETE FROM QNode n WHERE n.id IN (SELECT n.parent_id FROM QNode n WHERE n.children > :floor)"
        assert _sql(shadowed, QNode, postgresql.dialect()) == (
            "DELETE FROM q_node WHERE q_node.id IN (SELECT n.parent_id FROM q_node n WHERE n.children > :floor)"
        )

    def test_a_with_clause_introduces_the_statement(self):
        """The statement's kind is the verb after its ``WITH`` clause, and its target is found there too."""
        transpiled = transpile_jpql(
            "WITH roots AS (SELECT r.id FROM QNode r WHERE r.parent_id IS NULL) "
            "DELETE FROM QNode n WHERE n.parent_id IN (SELECT roots.id FROM roots)",
            QNode,
            postgresql.dialect(),
        )
        assert (transpiled.kind, transpiled.is_dml) == ("DELETE", True)
        assert transpiled.sql == (
            "WITH roots AS (SELECT r.id FROM q_node r WHERE r.parent_id IS NULL) "
            "DELETE FROM q_node WHERE q_node.parent_id IN (SELECT roots.id FROM roots)"
        )
        selected = transpile_jpql("WITH x AS (SELECT 1 AS one) SELECT n.id FROM QNode n", QNode)
        assert (selected.kind, selected.is_dml) == ("SELECT", False)
        assert [label for label, _type in selected.columns] == ["id"]  # the select list after the WITH clause

    def test_an_unknown_attribute_or_an_unterminated_literal_fails(self):
        with pytest.raises(InvalidQueryMethodError, match="no attribute or column 'nmae'"):
            _sql("SELECT i FROM Item i WHERE i.nmae = :name")
        with pytest.raises(InvalidQueryMethodError, match="Unterminated"):
            _sql("SELECT i FROM Item i WHERE i.name = 'x")

    def test_a_class_name_two_modules_share_resolves_in_the_entitys_module(self):
        class Isolated(DeclarativeBase):
            """A registry of its own, so the twin classes never reach Base's."""

        def mapped(name: str, module: str, table: str) -> type:
            namespace = {
                "__module__": module,
                "__tablename__": table,
                "__annotations__": {"id": Mapped[int]},
                "id": mapped_column(Integer, primary_key=True, autoincrement=False),
            }
            return type(name, (Isolated,), namespace)

        holder = mapped("Holder", "app.models", "holder")
        tags = [mapped("Tag", "app.models", "app_tag"), mapped("Tag", "vendor.models", "vendor_tag")]
        assert [tag.__module__ for tag in tags] == ["app.models", "vendor.models"]  # both alive: the registry is weak
        assert _sql("SELECT h.id FROM Holder h JOIN Tag t ON t.id = h.id", holder) == (
            "SELECT h.id FROM holder h JOIN app_tag t ON t.id = h.id"
        )
        stranger = mapped("Stranger", "third.models", "stranger")
        # Neither Tag is in the stranger's module: the name stays as written.
        assert _sql("SELECT s.id FROM Stranger s JOIN Tag t ON t.id = s.id", stranger) == (
            "SELECT s.id FROM stranger s JOIN Tag t ON t.id = s.id"
        )

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


class _Unresolved(Repository[Item, UUID]):
    @query("SELECT i FROM Item i WHERE i.role = :role")
    async def by_role(self, role: str) -> list[OnlyForTypeChecking]: ...  # type: ignore[name-defined]  # noqa: F821

    @query("SELECT i.name FROM Item i WHERE i.role = :role ORDER BY i.name")
    async def names_by_role(self, role: RoleOnlyForTypeChecking) -> list[str]: ...  # type: ignore[name-defined]  # noqa: F821


class TestAnUnresolvedAnnotation:
    async def test_the_query_runs_as_an_unannotated_one(
        self, executor: QueryExecutor, seeded_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level("WARNING", logger="pyfly.data.relational.sqlalchemy.query"):
            compiled = executor.compile_query_method(_Unresolved.by_role, Item)
        assert "query_method_annotations_unresolved" in caplog.text
        assert sorted(item.name for item in await compiled(seeded_session, role="admin")) == ["Alice", "Carol"]

    async def test_the_repository_builds(self, seeded_session: AsyncSession):
        repo = RepositoryBeanPostProcessor().after_init(_Unresolved(Item, seeded_session), "unresolved")
        assert sorted(item.name for item in await repo.by_role("user")) == ["Bob", "Dave"]

    async def test_an_unresolved_parameter_annotation_leaves_the_return_annotation_in_force(
        self, executor: QueryExecutor, seeded_session: AsyncSession, caplog: pytest.LogCaptureFixture
    ):
        """Each annotation resolves on its own: a parameter's that does not resolve no longer turns a query of
        names into an entity query."""
        with caplog.at_level("WARNING", logger="pyfly.data.relational.sqlalchemy.query"):
            compiled = executor.compile_query_method(_Unresolved.names_by_role, Item)
        unresolved = [
            record for record in caplog.records if record.getMessage() == "query_method_annotations_unresolved"
        ]
        assert [record.annotations for record in unresolved] == ["role"]  # type: ignore[attr-defined]
        assert await compiled(seeded_session, role="admin") == ["Alice", "Carol"]


class _Statements(Repository[Item, UUID]):
    """Statements of each verb, for what they read and write."""

    @modifying
    @query(
        "WITH low AS (SELECT id FROM q_items WHERE score < :floor) "
        "DELETE FROM q_items WHERE id IN (SELECT id FROM low)",
        native=True,
    )
    async def purge_below(self, floor: int) -> int: ...

    @query("CALL archive_items(:floor)", native=True)
    async def archive(self, floor: int) -> None: ...

    @query("WITH gone AS (DELETE FROM q_items WHERE score < :floor RETURNING id) SELECT id FROM gone", native=True)
    async def take_below(self, floor: int) -> list[UUID]: ...

    @query(
        "WITH top AS (SELECT i.id FROM Item i WHERE i.score > :floor) "
        "SELECT i FROM Item i WHERE i.id IN (SELECT id FROM top)"
    )
    async def above(self, floor: int) -> list[Item]: ...

    @query("SELECT i FROM Item i WHERE i.name = REPLACE(:name, '-', ' ')")
    async def named(self, name: str) -> list[Item]: ...

    @query("ANALYZE q_items", native=True)
    async def analyze(self) -> None: ...

    @query("PRAGMA table_info(q_items)", native=True)
    async def describe(self) -> None: ...

    @modifying
    @query("UPDATE Item i SET i.score = i.score + 1 WHERE i.role = :role")
    async def bump(self, role: str) -> int | None: ...

    @query("SELECT i.name FROM Item i WHERE i.id IN (:ids) ORDER BY i.name")
    async def names_of(self, ids: set[UUID] | frozenset[UUID]) -> list[str]: ...


class TestWhatAStatementDoes:
    """The verb after a ``WITH`` clause decides whether a statement is ``@modifying``; only a statement that
    changes nothing runs in a read unit."""

    @pytest.mark.parametrize(
        ("method", "modifying", "reads"),
        [
            ("purge_below", True, False),
            ("archive", False, False),
            ("take_below", False, False),
            ("above", False, True),
            ("named", False, True),
        ],
    )
    def test_what_each_statement_does(self, method: str, modifying: bool, reads: bool):
        compiled = QueryExecutor().compile_query_method(getattr(_Statements, method), Item)
        assert (compiled.is_modifying, compiled.reads) == (modifying, reads)

    async def test_a_statement_run_for_what_it_does_returns_none(
        self, executor: QueryExecutor, seeded_session: AsyncSession
    ):
        compiled = executor.compile_query_method(_Statements.analyze, Item)
        assert await compiled(seeded_session) is None

    async def test_a_with_clause_before_a_modifying_statement_counts_its_rows_on_sqlite(
        self, executor: QueryExecutor, seeded_session: AsyncSession
    ):
        """Python's sqlite3 reports no row count for a statement that starts with ``WITH``: SQLite's does."""
        compiled = executor.compile_query_method(_Statements.purge_below, Item)
        assert await compiled(seeded_session, floor=80) == 2  # Bob (70) and Dave (60)

    async def test_a_statement_run_for_what_it_does_closes_its_result(
        self, executor: QueryExecutor, seeded_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """Rows a statement run for what it does returns are never read, and its result is closed: a result left
        open beside the next statement is what MySQL refuses on one connection."""
        results: list[Any] = []
        execute = seeded_session.execute

        async def recorded(*args: Any, **kwargs: Any) -> Any:
            results.append(await execute(*args, **kwargs))
            return results[-1]

        monkeypatch.setattr(seeded_session, "execute", recorded)
        compiled = executor.compile_query_method(_Statements.describe, Item)
        assert await compiled(seeded_session) is None
        assert results and all(result.closed for result in results)

    async def test_a_modifying_statement_may_return_int_or_none(
        self, executor: QueryExecutor, seeded_session: AsyncSession
    ):
        compiled = executor.compile_query_method(_Statements.bump, Item)
        assert await compiled(seeded_session, role="admin") == 2

    @pytest.mark.parametrize("kind", [set, frozenset])
    async def test_a_set_of_uuids_binds_as_uuids(
        self, executor: QueryExecutor, seeded_session: AsyncSession, kind: type
    ):
        """A collection's values are typed by any of its elements, not only a sequence's first one: a set of UUIDs
        binds as ``Uuid`` (32 hex digits on SQLite), as a list of them does."""
        items = (await seeded_session.execute(text("SELECT id, name FROM q_items"))).all()
        by_name = {name: UUID(hex=str(key)) for key, name in items}
        compiled = executor.compile_query_method(_Statements.names_of, Item)
        assert await compiled(seeded_session, ids=kind([by_name["Bob"], by_name["Dave"]])) == ["Bob", "Dave"]


class TestLiteralsAsEachDialectReadsThem:
    """MySQL and MariaDB escape a quote with a backslash; PostgreSQL has ``E'...'`` and dollar-quoted literals."""

    def test_a_backslash_escaped_quote_is_a_mysql_literal(self):
        sql = "SELECT id FROM q_items WHERE name = 'it\\'s :x' AND role = :role"
        strings = [token.text for token in tokenize(sql, mysql.dialect()) if token.kind == "string"]
        assert strings == ["'it\\'s :x'"]

        @query(sql, native=True)
        async def mysql_query(self, role: str) -> list[int]: ...

        compiled = QueryExecutor().compile_query_method(mysql_query, Item)  # read as MySQL reads it: no error
        assert compiled.reads is True

    def test_the_standard_reading_comes_first_elsewhere(self):
        sql = "SELECT id FROM q_items WHERE name = 'C:\\' AND role = :role"
        strings = [token.text for token in tokenize(sql, postgresql.dialect()) if token.kind == "string"]
        assert strings == ["'C:\\'"]
        assert [token.text for token in tokenize(sql, mysql.dialect()) if token.kind == "bind"] == [":role"]

    def test_postgresql_escape_strings_and_dollar_quotes_are_literals(self):
        sql = "SELECT E'it\\'s :a', $$ it's :b $$, $body$ :c $body$, :d"
        tokens = tokenize(sql, postgresql.dialect())
        assert [token.text for token in tokens if token.kind == "string"] == [
            "E'it\\'s :a'",
            "$$ it's :b $$",
            "$body$ :c $body$",
        ]
        assert [token.text for token in tokens if token.kind == "bind"] == [":d"]


class TestBracketsAsEachDialectReadsThem:
    """``[...]`` quotes a name on SQL Server and SQLite only: elsewhere it builds or subscripts an array
    (PostgreSQL's ``ARRAY[:a, :b]``, ``tags[:i]``), and the parameters inside it are parameters. Reading it as a
    name on every dialect escaped them, so ``:a`` reached PostgreSQL as written."""

    ARRAYED = "SELECT id FROM q_items WHERE name = ANY(ARRAY[:a, :b]) AND score = scores[:i]"

    @pytest.mark.parametrize(
        "dialect", [None, postgresql.dialect(), mysql.dialect()], ids=["unknown", "postgresql", "mysql"]
    )
    def test_the_parameters_inside_brackets_are_parameters(self, dialect: Any):
        assert [token.text for token in tokenize(self.ARRAYED, dialect) if token.kind == "bind"] == [":a", ":b", ":i"]
        native = _native(self.ARRAYED, dialect)
        assert (native.sql, native.binds) == (self.ARRAYED, ("a", "b", "i"))
        assert set(text(native.sql).compile(dialect=postgresql.dialect()).params) == {"a", "b", "i"}

    def test_a_jpql_array_binds_its_parameters(self):
        jpql = "SELECT i.id FROM Item i WHERE i.name = ANY(ARRAY[:a, :b]) AND i.score > i.score"
        transpiled = transpile_jpql(jpql, Item, postgresql.dialect())
        assert transpiled.binds == ("a", "b")
        assert transpiled.sql == "SELECT i.id FROM q_items i WHERE i.name = ANY(ARRAY[:a, :b]) AND i.score > i.score"

    @pytest.mark.parametrize("dialect", [sqlite.dialect(), mssql.dialect()], ids=["sqlite", "mssql"])
    def test_a_bracketed_name_is_a_name_where_the_dialect_quotes_with_brackets(self, dialect: Any):
        sql = "SELECT name AS [at:noon] FROM q_items WHERE role = :role"
        assert [token.text for token in tokenize(sql, dialect) if token.kind == "quoted"] == ["[at:noon]"]
        native = _native(sql, dialect)
        assert native.binds == ("role",)
        assert set(text(native.sql).compile(dialect=dialect).params) == {"role"}

    def test_a_parameter_inside_brackets_is_checked_at_startup(self):
        @query("SELECT id FROM q_items WHERE name = ANY(ARRAY[:a, :nme])", native=True)
        async def arrayed(self, a: str, name: str) -> list[int]: ...

        with pytest.raises(InvalidQueryMethodError, match=":nme"):
            QueryExecutor().compile_query_method(arrayed, Item)


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
