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
"""What pyfly.data.relational.sqlalchemy.compat bridges between SQLAlchemy 2.0 and 2.1.

The suite runs on both lines (CI has a lane pinned to 2.0): each test holds on the line it runs on, and the
tests that need a 2.1 construct are skipped on 2.0.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import (
    JSON,
    Column,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    TypeDecorator,
    Uuid,
    column,
    select,
    table,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select
from sqlalchemy.types import NullType

from pyfly.data.relational.sqlalchemy.compat import drop_distinct_on_extension, foreign_key_target_table, python_type
from pyfly.data.relational.sqlalchemy.entity import NAMING_CONVENTION

_SYNTAX_EXTENSIONS = hasattr(Select, "ext")  # SQLAlchemy 2.1
_needs_extensions = pytest.mark.skipif(not _SYNTAX_EXTENSIONS, reason="SQLAlchemy 2.0 has no syntax extensions")

_ITEMS = table("items", column("id", Integer), column("name", String))


def _sql(statement: Any) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.asyncpg.dialect())).split())


class _Code(TypeDecorator[str]):
    impl = String
    cache_ok = True


class TestPythonType:
    @pytest.mark.parametrize(
        "column_type", [JSON(), postgresql.JSONB(), postgresql.JSON()], ids=["json", "jsonb", "pg-json"]
    )
    def test_a_json_column_holds_a_dict(self, column_type: Any) -> None:
        assert python_type(column_type) is dict

    @pytest.mark.parametrize(
        ("column_type", "expected"), [(Integer(), int), (String(), str), (LargeBinary(), bytes), (Uuid(), uuid.UUID)]
    )
    def test_a_known_python_type_is_answered(self, column_type: Any, expected: type) -> None:
        assert python_type(column_type) is expected

    @pytest.mark.parametrize("column_type", [_Code(), NullType(), postgresql.INET()], ids=["decorator", "null", "inet"])
    def test_an_unknown_python_type_raises_on_either_line(self, column_type: Any) -> None:
        with pytest.raises(NotImplementedError):
            python_type(column_type)


class TestDropDistinctOnExtension:
    def test_a_statement_without_the_extension_is_left_alone(self) -> None:
        statement = select(_ITEMS).distinct()
        drop_distinct_on_extension(statement)
        assert _sql(statement).startswith("SELECT DISTINCT items.id")

    @_needs_extensions
    def test_the_extension_is_taken_off(self) -> None:
        statement = select(_ITEMS).ext(postgresql.distinct_on(_ITEMS.c.name))
        statement._distinct = False
        drop_distinct_on_extension(statement)
        assert statement._pre_columns_clause is None
        assert _sql(statement) == "SELECT items.id, items.name FROM items"

    @_needs_extensions
    def test_another_extension_at_the_same_point_stays(self) -> None:
        from sqlalchemy.ext.compiler import compiles
        from sqlalchemy.sql import ClauseElement, SyntaxExtension

        class Hint(SyntaxExtension, ClauseElement):  # type: ignore[misc]
            __visit_name__ = "pyfly_compat_test_hint"
            _traverse_internals: list[Any] = []
            inherit_cache = True

            def apply_to_select(self, select_stmt: Select[Any]) -> None:
                select_stmt.apply_syntax_extension_point(lambda existing: [*existing, self], "pre_columns")

        @compiles(Hint)
        def _render(_element: Hint, _compiler: Any, **_kw: Any) -> str:
            return "/* hint */"

        statement = select(_ITEMS).ext(Hint()).ext(postgresql.distinct_on(_ITEMS.c.name))
        statement._distinct = False
        drop_distinct_on_extension(statement)
        assert isinstance(statement._pre_columns_clause, Hint)
        assert _sql(statement) == "SELECT /* hint */ items.id, items.name FROM items"


class TestForeignKeyTargetTable:
    @pytest.mark.parametrize("target", ["parent.id", "sales.parent.id"], ids=["table", "schema-table"])
    def test_a_dotted_target_names_its_table(self, target: str) -> None:
        assert foreign_key_target_table(ForeignKey(target)) == "parent"

    @_needs_extensions
    def test_a_table_name_with_a_dot_is_read_whole(self) -> None:
        """2.1's ``target_fullname`` raises for such a name; the naming convention names the constraint anyway."""
        metadata = MetaData(naming_convention=NAMING_CONVENTION)
        parent = Table("legacy.parent", metadata, Column("id", Integer, primary_key=True))
        child = Table(
            "child", metadata, Column("id", Integer, primary_key=True), Column("parent_id", ForeignKey(parent.c.id))
        )
        (foreign_key,) = child.c.parent_id.foreign_keys
        assert foreign_key_target_table(foreign_key) == "legacy.parent"
        (constraint,) = child.foreign_key_constraints
        assert constraint.name == "fk_child_parent_id_legacy.parent"
