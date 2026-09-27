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
"""The shared statement helpers, compiled for every dialect the framework must not break.

The live behavior runs on the backend matrix (``tests/integration/test_repository_semantics_matrix.py``);
here the statements are compiled for PostgreSQL, SQLite, MySQL, MariaDB, SQL Server and Oracle, which is how
a SQL Server or Oracle defect is caught without those servers (the audit's compile-only method):

- the ``LIMIT 1`` exists probe, never a ``COUNT`` and never a bare ``SELECT EXISTS`` (C129, C130);
- IN lists chunked to the dialect's limit (Oracle 1000 per list, SQL Server about 2100 parameters) and
  padded to powers of two, or one ``= ANY(:array)`` on PostgreSQL, so the prepared-statement cache sees a
  handful of statement texts (C139, C172); composite keys as row values, or an OR of ANDs on SQL Server (C134);
- orders with NULL placement and case folding rendered natively or emulated (C112), and the primary-key
  tie-breaker every paging path appends (C055);
- the delete strategy: a bulk ``DELETE`` only for a mapper without cascades, version column or delete
  listeners (C053, C133);
- fetch plans and lock modes (C137); joined collections turned into ``selectin`` loads for streams (C140).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String, event, select
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, joinedload, mapped_column, relationship, selectinload
from sqlalchemy.sql import Select

from pyfly.data.pageable import Order, Sort
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.statements import (
    LockMode,
    backend_name,
    bulk_delete_safe,
    chunked,
    exists_probe,
    in_criteria,
    in_list_limit,
    loader_options,
    order_expressions,
    padded,
    primary_key_orders,
    stream_safe,
)
from tests.support.contract_models import ContractChild, ContractLine, ContractParent, ContractVersioned


class StShelf(Base):
    __tablename__ = "st_shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    books: Mapped[list[StBook]] = relationship(lazy="joined", back_populates="shelf")


class StBook(Base):
    __tablename__ = "st_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("st_shelf.id"))
    shelf: Mapped[StShelf] = relationship(lazy="joined", back_populates="books")


class StPassive(Base):
    __tablename__ = "st_passive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    notes: Mapped[list[StPassiveNote]] = relationship(passive_deletes=True, cascade="all, delete-orphan")


class StPassiveNote(Base):
    __tablename__ = "st_passive_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    passive_id: Mapped[int] = mapped_column(ForeignKey("st_passive.id", ondelete="CASCADE"))


class StListened(Base):
    __tablename__ = "st_listened"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


@event.listens_for(StListened, "before_delete")
def _audit_delete(_mapper: Any, _connection: Any, _target: Any) -> None:
    pass


DIALECTS: dict[str, Dialect] = {
    "postgresql": postgresql.dialect(),
    "sqlite": sqlite.dialect(),
    "mysql": mysql.dialect(),
    "mariadb": mysql.dialect(is_mariadb=True),
    "mssql": mssql.dialect(),
    "oracle": oracle.dialect(),
}


def _sql(statement: Any, dialect: Dialect) -> str:
    return " ".join(str(statement.compile(dialect=dialect, compile_kwargs={"render_postcompile": True})).split())


def _binds(statement: Any, dialect: Dialect) -> int:
    compiled = statement.compile(dialect=dialect, compile_kwargs={"render_postcompile": True})
    return len(compiled.positiontup) if compiled.positiontup is not None else len(compiled.params)


# ---------------------------------------------------------------------------------------------------------
# IN lists
# ---------------------------------------------------------------------------------------------------------


class TestPaddingAndChunks:
    def test_padded_to_the_next_power_of_two_with_the_last_value(self) -> None:
        sizes = [len(padded(list(range(n)), 1000)) for n in range(1, 10)]
        assert sizes == [1, 2, 4, 4, 8, 8, 8, 8, 16]
        assert padded([7, 8, 9], 1000) == [7, 8, 9, 9]
        assert padded([], 1000) == []

    def test_padding_never_exceeds_the_limit(self) -> None:
        assert len(padded(list(range(600)), 1000)) == 1000
        assert len(padded(list(range(1000)), 1000)) == 1000

    def test_chunked(self) -> None:
        assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
        assert chunked([], 3) == []

    @pytest.mark.parametrize(
        ("name", "limit"),
        [("postgresql", 32767), ("mysql", 65535), ("mariadb", 65535), ("mssql", 2000), ("oracle", 1000)],
    )
    def test_the_in_list_limit_follows_the_dialect(self, name: str, limit: int) -> None:
        assert in_list_limit(DIALECTS[name]) == limit
        assert backend_name(DIALECTS[name]) == name

    def test_sqlite_allows_its_variable_limit(self) -> None:
        assert in_list_limit(DIALECTS["sqlite"]) in (999, 32766)


class TestInCriteria:
    def test_postgresql_binds_one_array_whatever_the_count(self) -> None:
        dialect = DIALECTS["postgresql"]
        texts = set()
        for count in (1, 3, 50, 40_000):
            criteria = in_criteria([ContractParent.id], [uuid.uuid4() for _ in range(count)], dialect)
            assert len(criteria) == 1
            statement = select(ContractParent.id).where(criteria[0])
            texts.add(str(statement.compile(dialect=dialect)))
            assert _binds(statement, dialect) == 1
        assert len(texts) == 1
        assert "= ANY" in texts.pop()

    def test_other_dialects_pad_the_list_so_few_texts_exist(self) -> None:
        dialect = DIALECTS["sqlite"]
        texts = {
            _sql(select(ContractChild.id).where(*in_criteria([ContractChild.id], list(range(n)), dialect)), dialect)
            for n in range(1, 65)
        }
        assert len(texts) == 7  # 1, 2, 4, 8, 16, 32 and 64 binds

    @pytest.mark.parametrize(("name", "limit"), [("oracle", 1000), ("mssql", 2000)])
    def test_long_lists_are_chunked_to_the_dialect_limit(self, name: str, limit: int) -> None:
        dialect = DIALECTS[name]
        criteria = in_criteria([ContractChild.id], list(range(2500)), dialect)
        assert len(criteria) == -(-2500 // limit)
        for criterion in criteria:
            assert _binds(select(ContractChild.id).where(criterion), dialect) <= limit

    def test_composite_keys_are_row_values(self) -> None:
        keys = [("A", 1), ("A", 2), ("B", 1)]
        for name in ("postgresql", "sqlite", "mysql", "mariadb", "oracle"):
            dialect = DIALECTS[name]
            (criterion,) = in_criteria([ContractLine.order_code, ContractLine.line_no], keys, dialect)
            sql = _sql(select(ContractLine.sku).where(criterion), dialect)
            assert "(contract_line.order_code, contract_line.line_no) IN" in sql, name

    def test_sql_server_gets_an_or_of_ands_for_composite_keys(self) -> None:
        dialect = DIALECTS["mssql"]
        keys = [(f"K{n}", n) for n in range(1500)]
        criteria = in_criteria([ContractLine.order_code, ContractLine.line_no], keys, dialect)
        assert len(criteria) == 2  # 1000 keys per statement: 2000 parameters
        sql = _sql(select(ContractLine.sku).where(criteria[1]), dialect)
        assert " IN " not in sql
        assert "contract_line.order_code = " in sql and " OR " in sql
        assert all(_binds(select(ContractLine.sku).where(c), dialect) <= 2000 for c in criteria)

    def test_an_empty_list_matches_nothing_without_a_statement(self) -> None:
        assert in_criteria([ContractChild.id], [], DIALECTS["sqlite"]) == []


# ---------------------------------------------------------------------------------------------------------
# Exists probe
# ---------------------------------------------------------------------------------------------------------


class TestExistsProbe:
    @pytest.mark.parametrize(
        ("name", "limit"),
        [
            ("postgresql", "LIMIT"),
            ("sqlite", "LIMIT"),
            ("mysql", "LIMIT"),
            ("mariadb", "LIMIT"),
            ("mssql", "TOP"),
            ("oracle", "FETCH FIRST"),
        ],
    )
    def test_a_probe_selects_a_literal_with_a_limit_of_one(self, name: str, limit: str) -> None:
        sql = _sql(exists_probe(ContractParent, ContractParent.name == "x"), DIALECTS[name])
        assert limit in sql
        assert "count(" not in sql.lower()
        assert "EXISTS" not in sql.upper()
        assert "contract_parent.name" in sql

    def test_an_entity_select_becomes_a_probe_with_its_criteria(self) -> None:
        base: Select[Any] = select(ContractParent).where(ContractParent.active.is_(True)).order_by(ContractParent.name)
        sql = _sql(exists_probe(base, ContractParent.name == "x"), DIALECTS["postgresql"])
        assert sql.startswith("SELECT 1 FROM contract_parent WHERE")
        assert "contract_parent.active IS true" in sql and "contract_parent.name =" in sql
        assert "ORDER BY" not in sql and sql.endswith("LIMIT %(param_1)s")


# ---------------------------------------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------------------------------------


def _ordered(sort: Sort, name: str, model: type = StShelf) -> str:
    dialect = DIALECTS[name]
    return _sql(select(model.id).order_by(*order_expressions(model, sort, dialect)), dialect)  # type: ignore[attr-defined]


class TestOrdering:
    def test_native_null_placement_where_the_dialect_has_it(self) -> None:
        sort = Sort.by(Order.asc("name").nulls_last())
        assert _ordered(sort, "postgresql").endswith("ORDER BY st_shelf.name ASC NULLS LAST")
        assert _ordered(sort, "oracle").endswith("ORDER BY st_shelf.name ASC NULLS LAST")
        assert "NULLS LAST" in _ordered(sort, "sqlite")

    @pytest.mark.parametrize("name", ["mysql", "mariadb", "mssql"])
    def test_emulated_null_placement_elsewhere(self, name: str) -> None:
        last = _ordered(Sort.by(Order.desc("name").nulls_last()), name)
        assert "NULLS" not in last
        assert "CASE WHEN (st_shelf.name IS NULL) THEN" in last and "st_shelf.name DESC" in last
        first = _ordered(Sort.by(Order.desc("name").nulls_first()), name)
        assert first != last

    def test_ignore_case_orders_by_the_lower_cased_value(self) -> None:
        assert "lower(st_shelf.name) DESC" in _ordered(Sort.by(Order.desc("name").ignoring_case()), "postgresql")

    def test_native_orders_are_plain(self) -> None:
        assert _ordered(Sort.by("name"), "mysql").endswith("ORDER BY st_shelf.name ASC")

    def test_names_go_through_the_resolver(self) -> None:
        resolver = PropertyResolver.for_entity(StShelf, allowed=("id",))
        with pytest.raises(InvalidPropertyError):
            order_expressions(StShelf, Sort.by("name"), DIALECTS["sqlite"], resolver=resolver)

    def test_the_primary_key_breaks_ties_unless_the_sort_has_it(self) -> None:
        dialect = DIALECTS["mssql"]
        sql = _sql(select(ContractLine.sku).order_by(*primary_key_orders(ContractLine, Sort.by("sku"))), dialect)
        assert sql.endswith("ORDER BY contract_line.order_code ASC, contract_line.line_no ASC")
        assert primary_key_orders(ContractLine, Sort.by("line_no", "order_code")) == []
        assert len(primary_key_orders(ContractLine, Sort.by(Order.desc("line_no")))) == 1


# ---------------------------------------------------------------------------------------------------------
# Delete strategy
# ---------------------------------------------------------------------------------------------------------


class TestDeleteStrategy:
    def test_cascades_versions_and_listeners_need_the_orm(self) -> None:
        assert not bulk_delete_safe(ContractParent)  # cascade="all, delete-orphan"
        assert not bulk_delete_safe(ContractVersioned)  # version_id_col
        assert not bulk_delete_safe(StListened)  # a before_delete listener
        assert not bulk_delete_safe(StShelf)  # a one-to-many the ORM would null out

    def test_plain_mappers_and_database_cascades_delete_in_bulk(self) -> None:
        assert bulk_delete_safe(ContractLine)
        assert bulk_delete_safe(ContractChild)  # many-to-one only
        assert bulk_delete_safe(StPassive)  # passive_deletes: the database cascades


# ---------------------------------------------------------------------------------------------------------
# Fetch plans, locks, streams
# ---------------------------------------------------------------------------------------------------------


class TestFetchPlans:
    def test_names_attributes_and_options_become_loader_options(self) -> None:
        by_name = loader_options(ContractParent, "children")
        by_attribute = loader_options(ContractParent, [ContractParent.children])
        explicit = joinedload(ContractParent.children)
        assert len(by_name) == len(by_attribute) == 1
        assert loader_options(ContractParent, explicit) == [explicit]
        assert loader_options(ContractParent, None) == []
        assert loader_options(ContractParent, ()) == []
        sql = _sql(select(ContractParent).options(*by_name), DIALECTS["postgresql"])
        assert "contract_child" not in sql  # selectin: a second statement, not a join

    def test_a_dotted_name_loads_a_path(self) -> None:
        (option,) = loader_options(ContractChild, "parent.children")
        steps = [getattr(step, "key", None) for step in option.context[-1].path]
        assert [key for key in steps if key] == ["parent", "children"]

    @pytest.mark.parametrize("name", ["nope", "name", "parent.nope"])
    def test_anything_but_a_relationship_is_refused(self, name: str) -> None:
        model = ContractChild if name.startswith("parent") else ContractParent
        with pytest.raises(InvalidPropertyError):
            loader_options(model, name)

    def test_other_values_are_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            loader_options(ContractParent, [42])


class TestLockModes:
    @pytest.mark.parametrize(
        ("mode", "clause"),
        [
            (LockMode.PESSIMISTIC_WRITE, "FOR UPDATE"),
            (LockMode.PESSIMISTIC_READ, "FOR SHARE"),
            (LockMode.PESSIMISTIC_WRITE_NOWAIT, "FOR UPDATE NOWAIT"),
            (LockMode.PESSIMISTIC_WRITE_SKIP_LOCKED, "FOR UPDATE SKIP LOCKED"),
        ],
    )
    def test_lock_modes_render_on_postgresql(self, mode: LockMode, clause: str) -> None:
        statement = select(ContractParent.id).with_for_update(**mode.for_update)
        assert _sql(statement, DIALECTS["postgresql"]).endswith(clause)


class TestStreams:
    def test_joined_collections_load_with_selectin_in_a_stream(self) -> None:
        statement = stream_safe(select(StShelf), StShelf)
        sql = _sql(statement, DIALECTS["postgresql"])
        assert "st_book" not in sql  # the books come from a second, per-batch statement

    def test_a_joined_many_to_one_keeps_its_join_but_not_its_target_collections(self) -> None:
        sql = _sql(stream_safe(select(StBook), StBook), DIALECTS["postgresql"])
        assert "LEFT OUTER JOIN st_shelf" in sql
        assert "st_book_1" not in sql  # the shelf's books are not joined back in

    def test_a_mapper_without_joined_collections_is_unchanged(self) -> None:
        statement = select(ContractParent)
        assert stream_safe(statement, ContractParent) is statement

    def test_explicit_selectin_options_survive(self) -> None:
        statement = stream_safe(select(StShelf).options(selectinload(StShelf.books)), StShelf)
        assert "st_book" not in _sql(statement, DIALECTS["sqlite"])
