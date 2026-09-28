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

import re
import uuid
import warnings
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String, create_engine, event, func, select
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import (
    Mapped,
    Session,
    aliased,
    contains_eager,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    with_loader_criteria,
)
from sqlalchemy.sql import Select

from pyfly.data.pageable import Order, Sort
from pyfly.data.property_resolver import InvalidPropertyError, PropertyResolver
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.statements import (
    RESERVED_BINDS,
    LockMode,
    backend_name,
    bulk_delete_safe,
    chunked,
    distinct_entity_count,
    distinct_entity_page,
    exists_probe,
    in_criteria,
    in_list_limit,
    joins_rows,
    loader_options,
    loads_per_batch,
    order_expressions,
    padded,
    primary_key_orders,
    row_count,
    stream_safe,
)
from tests.support.contract_models import ContractChild, ContractLine, ContractParent, ContractVersioned

_SYNTAX_EXTENSIONS = hasattr(Select, "ext")  # SQLAlchemy 2.1


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


class StOwner(Base):
    __tablename__ = "st_owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class StCar(Base):
    """A joined many-to-one to a target that loads nothing else: its rows need no other statement."""

    __tablename__ = "st_car"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("st_owner.id"))
    owner: Mapped[StOwner] = relationship(lazy="joined")


class StBike(Base):
    """A ``selectin`` many-to-one: its rows need one more statement per batch."""

    __tablename__ = "st_bike"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("st_owner.id"))
    owner: Mapped[StOwner] = relationship(lazy="selectin")


class StListened(Base):
    __tablename__ = "st_listened"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


@event.listens_for(StListened, "before_delete")
def _audit_delete(_mapper: Any, _connection: Any, _target: Any) -> None:
    pass


DIALECTS: dict[str, Dialect] = {
    # The driver the postgresql extra installs, named: a bare postgresql.dialect() is SQLAlchemy's default driver,
    # psycopg2 on 2.0 and psycopg 3 on 2.1, and the two render bound parameters differently.
    "postgresql": postgresql.asyncpg.dialect(),
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

    def test_composite_keys_go_a_thousand_per_list(self) -> None:
        """A long row-value list exhausts PostgreSQL's parser stack; 1000 rows is every dialect's safe size."""
        keys = [(f"K{n}", n) for n in range(2500)]
        for name in ("postgresql", "mysql", "sqlite"):
            criteria = in_criteria([ContractLine.order_code, ContractLine.line_no], keys, DIALECTS[name])
            assert len(criteria) == 3, name

    def test_sql_server_gets_an_or_of_ands_for_composite_keys(self) -> None:
        dialect = DIALECTS["mssql"]
        keys = [(f"K{n}", n) for n in range(1500)]
        criteria = in_criteria([ContractLine.order_code, ContractLine.line_no], keys, dialect)
        assert len(criteria) == 2  # 1000 keys per statement: 2000 parameters
        sql = _sql(select(ContractLine.sku).where(criteria[1]), dialect)
        assert " IN " not in sql
        assert "contract_line.order_code = " in sql and " OR " in sql
        assert all(_binds(select(ContractLine.sku).where(c), dialect) <= 2000 for c in criteria)

    @pytest.mark.parametrize(("name", "limit"), [("sqlite", 32766), ("mssql", 2000), ("mysql", 65535)])
    def test_a_chunk_leaves_room_for_the_statements_other_binds(self, name: str, limit: int) -> None:
        """A limit that counts every bind of a statement: the padded chunk plus *reserved* stays within it."""
        dialect = DIALECTS[name]
        if name == "sqlite" and in_list_limit(dialect) != limit:
            pytest.skip("this SQLite library predates the 32766 variable limit")
        values = list(range(2 * limit + 7))
        for reserved in (RESERVED_BINDS, 100):
            criteria = in_criteria([ContractChild.id], values, dialect, reserved=reserved)
            assert all(_binds(select(ContractChild.id).where(c), dialect) <= limit - reserved for c in criteria)
            assert len(criteria) == 3

    def test_oracles_limit_is_per_list_so_nothing_is_reserved(self) -> None:
        criteria = in_criteria([ContractChild.id], list(range(2500)), DIALECTS["oracle"], reserved=100)
        assert [_binds(select(ContractChild.id).where(c), DIALECTS["oracle"]) for c in criteria] == [1000, 1000, 512]

    def test_reserving_the_whole_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            in_criteria([ContractChild.id], [1, 2], DIALECTS["mssql"], reserved=2000)

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
        assert "ORDER BY" not in sql and sql.endswith("LIMIT $2::INTEGER")


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

    def test_ignore_case_folds_only_strings(self) -> None:
        assert _ordered(Sort.by(Order.desc("id").ignoring_case()), "postgresql").endswith("ORDER BY st_shelf.id DESC")
        folded = _ordered(Sort.by(Order.asc("id").ignoring_case()), "postgresql", model=ContractParent)
        assert "lower(" not in folded  # a UUID (a type decorator over a string type on some backends) is not text

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
# Entities a join repeats
# ---------------------------------------------------------------------------------------------------------


class StStaff(Base):
    __tablename__ = "st_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "staff"}


class StManager(StStaff):
    __tablename__ = "st_manager"

    id: Mapped[int] = mapped_column(ForeignKey("st_staff.id"), primary_key=True)
    __mapper_args__ = {"polymorphic_identity": "manager"}


def _joined_parents() -> Select[Any]:
    return select(ContractParent).join(ContractParent.children).where(ContractChild.label == "hit")


class TestDistinctEntities:
    def test_a_join_or_another_from_repeats_entities(self) -> None:
        assert joins_rows(_joined_parents(), ContractParent)
        assert joins_rows(select(ContractParent).where(ContractChild.parent_id == ContractParent.id), ContractParent)
        assert joins_rows(select(ContractLine).where(ContractLine.sku == ContractChild.label), ContractLine)

    def test_criteria_subqueries_and_inheritance_do_not(self) -> None:
        assert not joins_rows(select(ContractParent).where(ContractParent.name == "a"), ContractParent)
        hit = ContractParent.children.any(ContractChild.label == "hit")
        assert not joins_rows(select(ContractParent).where(hit), ContractParent)
        assert not joins_rows(
            select(ContractParent).where(ContractParent.id.in_(select(ContractChild.parent_id))), ContractParent
        )
        assert not joins_rows(select(StManager).where(StManager.id > 0), StManager)  # its own two tables

    def test_a_join_along_a_many_to_one_does_not(self) -> None:
        """A many-to-one matches at most one row per entity, so a page of its join needs no DISTINCT."""
        spanish = ContractParent.name == "a"
        assert not joins_rows(select(ContractChild).join(ContractChild.parent).where(spanish), ContractChild)
        assert not joins_rows(select(ContractChild).outerjoin(ContractChild.parent), ContractChild)
        assert not joins_rows(
            select(ContractChild).join(ContractParent, ContractChild.parent).where(spanish), ContractChild
        )
        assert not joins_rows(select(StCar).join(StCar.owner).where(StOwner.id > 1), StCar)
        # The shelf's books are a collection: joining them from the book's shelf repeats the book.
        assert joins_rows(select(StBook).join(StBook.shelf).join(StShelf.books.of_type(aliased(StBook))), StBook)

    def test_a_join_it_cannot_tell_is_taken_to_repeat_entities(self) -> None:
        """An ON clause of its own, a join from the other side of a many-to-one (one-to-many from the entity), or
        a WHERE that names a table the joins did not bring in."""
        by_hand = select(ContractChild).join(ContractParent, ContractChild.parent_id == ContractParent.id)
        assert joins_rows(by_hand, ContractChild)
        assert joins_rows(select(ContractParent).join(ContractChild, ContractChild.parent), ContractParent)
        other = select(ContractChild).join(ContractChild.parent).where(ContractLine.sku == ContractChild.label)
        assert joins_rows(other, ContractChild)

    @pytest.mark.parametrize("name", ["postgresql", "sqlite", "mysql", "mariadb", "oracle"])
    def test_a_page_is_cut_from_the_distinct_keys_with_their_order_keys(self, name: str) -> None:
        dialect = DIALECTS[name]
        ordered = _joined_parents().order_by(
            *order_expressions(ContractParent, Sort.by(Order.desc("name").ignoring_case().nulls_last()), dialect),
            *primary_key_orders(ContractParent, Sort.unsorted()),
        )
        sql = _sql(distinct_entity_page(ordered, ContractParent, offset=4, limit=2), dialect)
        assert "JOIN (SELECT DISTINCT contract_parent.id AS pyfly_k0, " in sql
        assert "lower(contract_parent.name) AS pyfly_o" in sql  # the order keys the DISTINCT selects
        # The entities are read through the statement's own join and criteria, joined to the page's keys.
        joined = "FROM contract_parent (INNER )?JOIN contract_child ON contract_parent.id = contract_child.parent_id "
        assert re.search(joined + r"(INNER )?JOIN \(SELECT DISTINCT", sql)  # MySQL says INNER JOIN
        assert "ON contract_parent.id = pyfly_page.pyfly_k0 WHERE contract_child.label = " in sql
        # The outer query orders by the same keys, read from the page: the name descending, then the key.
        outer = sql.rsplit(" ORDER BY ", 1)[1]
        last = len(outer.split(", ")) - 1
        assert re.search(r"pyfly_page\.pyfly_o\d DESC", outer) and outer.endswith(f"pyfly_page.pyfly_o{last} ASC")

    def test_the_entities_are_read_through_the_statements_own_froms(self) -> None:
        """The statement that reads the page's entities is the statement itself joined to the page's keys, so the
        rows a ``contains_eager`` loads come from its own join and criteria. Read by a bare ``SELECT`` of the
        entity, the collection's table was a FROM of its own there: a cartesian product that gave each entity
        every row of that table."""
        dialect = DIALECTS["postgresql"]
        eager = _joined_parents().options(contains_eager(ContractParent.children)).order_by(ContractParent.name)
        sql = _sql(distinct_entity_page(eager, ContractParent, limit=2), dialect)
        entities = sql.split(" (SELECT DISTINCT ")[0]
        assert "contract_child.label" in entities.split(" FROM ")[0]  # the collection's rows come with the page
        joined = "FROM contract_parent JOIN contract_child ON contract_parent.id = contract_child.parent_id JOIN"
        assert entities.endswith(joined)
        assert sql.endswith("WHERE contract_child.label = $1::VARCHAR ORDER BY pyfly_page.pyfly_o0")
        # A FROM that starts with another table: the page's keys join the entity, which that table does not name.
        correlated = (
            select(ContractParent)
            .select_from(ContractChild)
            .where(ContractChild.parent_id == ContractParent.id)
            .order_by(ContractParent.name)
        )
        sql = _sql(distinct_entity_page(correlated, ContractParent, limit=2), dialect)
        assert sql.split(" (SELECT DISTINCT ")[0].endswith("FROM contract_child, contract_parent JOIN")

    @pytest.mark.parametrize(
        "spell",
        [
            pytest.param(lambda statement: statement.distinct(ContractParent.name), id="distinct-expressions"),
            pytest.param(
                lambda statement: statement.ext(postgresql.distinct_on(ContractParent.name)),
                id="distinct_on-extension",
                marks=pytest.mark.skipif(not _SYNTAX_EXTENSIONS, reason="SQLAlchemy 2.0 has no syntax extensions"),
            ),
        ],
    )
    def test_a_distinct_on_shapes_only_the_keys(self, spell: Any) -> None:
        """PostgreSQL's ``DISTINCT ON`` picks the page's keys like any ``DISTINCT``, and the entities are read without
        it. SQLAlchemy 2.0 spells it ``distinct(*expressions)`` (deprecated on 2.1), 2.1 as a syntax extension that
        resetting ``_distinct`` leaves behind: the entities' ``SELECT`` rendered ``SELECT ON (...)``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # 2.1 deprecates distinct(*expressions)
            statement = spell(_joined_parents()).order_by(ContractParent.name)
        dialect = DIALECTS["postgresql"]
        entities, keys = _sql(distinct_entity_page(statement, ContractParent, limit=2), dialect).split(" JOIN (", 1)
        assert keys.startswith("SELECT DISTINCT ON (contract_parent.name) contract_parent.id AS pyfly_k0, ")
        assert entities.startswith("SELECT contract_parent.name, ") and " ON (" not in entities.split(" FROM ")[0]
        count = _sql(distinct_entity_count(statement, ContractParent), dialect)
        assert "(SELECT DISTINCT ON (contract_parent.name) contract_parent.id " in count

    def test_the_statements_own_distinct_grouping_and_limit_shape_only_the_keys(self) -> None:
        """A statement's ``DISTINCT``, ``GROUP BY``, ``HAVING``, ``LIMIT`` and ``OFFSET`` decide which entities the
        page's keys hold; the statement that reads the entities by those keys leaves them out (a ``DISTINCT`` or a
        ``GROUP BY`` there could not order by the page's keys, and a ``LIMIT`` would count joined rows)."""
        grouped = (
            _joined_parents()
            .group_by(ContractParent.id)
            .having(func.count(ContractChild.id) > 1)
            .distinct()
            .limit(9)
            .offset(3)
            .order_by(ContractParent.name)
        )
        sql = _sql(distinct_entity_page(grouped, ContractParent, offset=2, limit=2), DIALECTS["postgresql"])
        keys, entities = sql.split(") AS pyfly_page ")
        assert "GROUP BY contract_parent.id HAVING count(contract_child.id) > " in keys
        assert keys.endswith("LIMIT $3::INTEGER OFFSET $4::INTEGER")  # the page's cut, not the statement's
        assert sql.startswith("SELECT contract_parent.")  # no DISTINCT on the entities
        assert not any(clause in entities for clause in ("DISTINCT", "GROUP BY", "HAVING", "LIMIT", "OFFSET"))
        assert grouped._distinct and grouped._having_criteria and grouped._limit_clause is not None  # unchanged

    def test_sql_server_orders_the_derived_table_only_when_it_is_cut(self) -> None:
        server = mssql.dialect()
        server._supports_offset_fetch = True  # type: ignore[attr-defined]  # SQL Server 2012 and later
        ordered = _joined_parents().order_by(ContractParent.name.asc(), ContractParent.id.asc())
        cut = _sql(distinct_entity_page(ordered, ContractParent, offset=2, limit=2), server)
        assert (
            "ORDER BY pyfly_o0 ASC, pyfly_o1 ASC OFFSET :param_1 ROWS FETCH FIRST :param_2 ROWS ONLY) AS pyfly_page"
            in cut
        )
        whole = _sql(distinct_entity_page(ordered, ContractParent), server)
        assert "ORDER BY" not in whole.split(") AS pyfly_page")[0]
        assert whole.endswith("ORDER BY pyfly_page.pyfly_o0 ASC, pyfly_page.pyfly_o1 ASC")

    def test_composite_keys_join_on_every_key_column(self) -> None:
        statement = select(ContractLine).where(ContractLine.sku == ContractChild.label).order_by(ContractLine.sku)
        sql = _sql(distinct_entity_page(statement, ContractLine, limit=5), DIALECTS["sqlite"])
        assert "contract_line.order_code = pyfly_page.pyfly_k0 AND contract_line.line_no = pyfly_page.pyfly_k1" in sql

    def test_the_count_counts_distinct_keys(self) -> None:
        sql = _sql(
            distinct_entity_count(_joined_parents().order_by(ContractParent.name), ContractParent), DIALECTS["mssql"]
        )
        assert sql.startswith("SELECT count(*) AS count_1 FROM (SELECT DISTINCT contract_parent.id AS id FROM")
        assert "ORDER BY" not in sql

    def test_the_statement_that_reads_the_entities_carries_their_plan_and_options(self) -> None:
        """A subquery's loader options are never applied: the statement's fetch plan and execution options go on
        the statement that reads the entities, or the plan is silently dropped."""
        plan = selectinload(ContractParent.children)
        statement = _joined_parents().options(plan).execution_options(populate_existing=True).order_by("name")
        page = distinct_entity_page(statement, ContractParent, limit=2)
        assert plan in page._with_options
        assert page.get_execution_options()["populate_existing"] is True
        unplanned = distinct_entity_page(_joined_parents().order_by("name"), ContractParent, limit=2)
        assert unplanned._with_options == () and "populate_existing" not in unplanned.get_execution_options()

    @pytest.mark.parametrize(
        ("name", "clause"),
        [
            ("postgresql", "FOR UPDATE OF contract_parent NOWAIT"),
            ("oracle", "FOR UPDATE OF contract_parent.id NOWAIT"),
            ("mysql", "FOR UPDATE NOWAIT"),  # OF needs MySQL 8, which the dialect learns when it connects
        ],
    )
    def test_a_lock_moves_off_the_distinct_keys_onto_the_entities(self, name: str, clause: str) -> None:
        """PostgreSQL and Oracle refuse FOR UPDATE on a DISTINCT: the keys are read unlocked, and the lock takes
        the rows of the entities the page reads (their table, unless the statement named what to lock, which may
        be a table it joins: the statement that reads the entities has its joins)."""
        dialect = DIALECTS[name]
        locked = _joined_parents().order_by(ContractParent.name).with_for_update(nowait=True)
        sql = _sql(distinct_entity_page(locked, ContractParent, offset=2, limit=2), dialect)
        keys, entities = sql.split("pyfly_page ON ")
        assert "FOR UPDATE" not in keys
        assert entities.endswith(clause)
        shared = _joined_parents().order_by(ContractParent.name).with_for_update(read=True, of=ContractChild)
        sql = _sql(distinct_entity_page(shared, ContractParent, limit=2), DIALECTS["postgresql"])
        assert sql.endswith("FOR SHARE OF contract_child")  # what the statement named, a table it joins
        assert "FROM contract_parent JOIN contract_child ON contract_parent.id = contract_child.parent_id JOIN (" in sql
        assert "FOR UPDATE" not in _sql(distinct_entity_count(locked, ContractParent), dialect)
        assert locked._for_update_arg is not None  # the statement itself is unchanged

    @pytest.mark.parametrize("count", [row_count, lambda s: distinct_entity_count(s, ContractParent)])
    def test_a_count_keeps_the_loader_criteria_and_execution_options_but_no_order_or_lock(self, count: Any) -> None:
        """The ORM applies no option to a subquery: the loader criteria the counted statement carries go on the
        COUNT itself, so it counts what the statement reads; its fetch plan does not (the ORM refuses a loader
        option on a statement without the entity)."""
        criteria = with_loader_criteria(ContractParent, ContractParent.active.is_(True))
        statement = (
            _joined_parents()
            .options(criteria, selectinload(ContractParent.children))
            .execution_options(pyfly_probe="spec")
            .order_by(ContractParent.name)
            .with_for_update()
        )
        counted = count(statement)
        assert counted._with_options == (criteria,)
        assert counted.get_execution_options()["pyfly_probe"] == "spec"
        sql = _sql(counted, DIALECTS["postgresql"])
        assert sql.startswith("SELECT count(*) AS count_1 FROM (SELECT ")
        assert "ORDER BY" not in sql and "FOR UPDATE" not in sql
        plain = count(_joined_parents())
        assert plain._with_options == () and plain.get_execution_options() == {}


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

    @pytest.mark.parametrize("hook", ["before_flush", "after_flush", "after_flush_postexec", "persistent_to_deleted"])
    def test_a_session_that_watches_its_flushes_needs_the_orm(self, hook: str) -> None:
        """A flush listener (a common audit of ``session.deleted``) never sees the rows a bulk DELETE removes."""
        session = Session()
        assert bulk_delete_safe(ContractLine, session)
        event.listen(session, hook, lambda *_args: None)
        try:
            assert not bulk_delete_safe(ContractLine, session)
        finally:
            session.close()


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

    def test_what_a_batch_of_rows_loads_with_statements_of_its_own(self) -> None:
        assert not loads_per_batch(ContractParent)  # lazy loads only
        assert not loads_per_batch(StCar)  # a joined many-to-one comes with the row
        assert loads_per_batch(StBike)  # selectin
        assert loads_per_batch(StShelf)  # a joined collection, which a stream loads with selectin
        assert loads_per_batch(StBook)  # the shelf's joined books, behind its joined many-to-one

    def test_explicit_selectin_options_survive(self) -> None:
        statement = stream_safe(select(StShelf).options(selectinload(StShelf.books)), StShelf)
        assert "st_book" not in _sql(statement, DIALECTS["sqlite"])


# ---------------------------------------------------------------------------------------------------------
# The SQLAlchemy internals the helpers read
# ---------------------------------------------------------------------------------------------------------


class TestSqlAlchemyInternals:
    """The helpers read a few SQLAlchemy internals that have no public equivalent. These tests pin their shape,
    so an upgrade that renames or reshapes one fails here, by name, instead of changing what a page, a lock or
    a stream does."""

    def test_the_select_internals(self) -> None:
        plan = selectinload(ContractParent.children)
        named = ContractParent.name == "a"
        statement = (
            select(ContractChild)
            .join(ContractChild.parent)
            .where(named)
            .order_by(ContractChild.label.desc())
            .options(plan)
            .with_for_update(nowait=True)
        )
        ((target, onclause, left, flags),) = statement._setup_joins  # joins_rows
        assert target is ContractChild.parent and onclause is None and left is None
        assert flags == {"isouter": False, "full": False}
        assert statement._from_obj == ()
        assert statement.whereclause is not None
        assert [source._deannotate() for source in statement.whereclause._from_objects] == [ContractParent.__table__]
        assert len(statement._order_by_clauses) == 1  # distinct_entity_page
        assert statement._with_options == (plan,)  # distinct_entity_page, row_count
        lock = statement._for_update_arg
        assert lock is not None and (lock.read, lock.nowait, lock.skip_locked, lock.key_share, lock.of) == (
            False,
            True,
            False,
            False,
            None,
        )
        copy = statement._generate()  # _unlocked
        assert copy is not statement and copy._for_update_arg is lock
        assert select(ContractChild)._for_update_arg is None
        plain = select(ContractChild)  # _entity_rows
        assert (plain._distinct, plain._distinct_on, plain._having_criteria) == (False, (), ())
        shaped = plain.group_by(ContractChild.parent_id).having(named).distinct()
        assert (shaped._distinct, shaped._distinct_on, len(shaped._having_criteria)) == (True, (), 1)

    @pytest.mark.skipif(not _SYNTAX_EXTENSIONS, reason="SQLAlchemy 2.0 has no syntax extensions")
    def test_the_distinct_on_extension(self) -> None:
        """compat.drop_distinct_on_extension reads 2.1's ``_pre_columns_clause``, where ``ext(distinct_on(...))``
        goes (``_distinct_on`` stays empty)."""
        from sqlalchemy.dialects.postgresql.ext import DistinctOnClause

        statement = select(ContractParent).ext(postgresql.distinct_on(ContractParent.name))
        assert statement._distinct and statement._distinct_on == ()
        assert isinstance(statement._pre_columns_clause, DistinctOnClause)
        assert select(ContractParent)._pre_columns_clause is None

    def test_an_orm_result_says_when_it_must_be_made_unique(self) -> None:
        """unique_entities and stream_all read ``_unique_filter_state``: set on a result whose joined eager load
        of a collection repeats its entities, and on the streamed result derived from it."""
        engine = create_engine("sqlite://")
        tables = [ContractParent.__table__, ContractChild.__table__]
        Base.metadata.create_all(engine, tables=tables)  # type: ignore[arg-type]
        try:
            with Session(engine) as session:
                joined = session.execute(select(ContractParent).options(joinedload(ContractParent.children)))
                assert joined._unique_filter_state is not None
                assert session.execute(select(ContractParent))._unique_filter_state is None
        finally:
            engine.dispose()

    async def test_a_streamed_scalar_result_keeps_the_mark(self) -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        tables = [ContractParent.__table__, ContractChild.__table__]
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
            async with AsyncSession(engine) as session:
                planned = select(ContractParent).options(joinedload(ContractParent.children))
                streamed = await session.stream_scalars(planned)
                assert streamed._unique_filter_state is not None
                await streamed.close()
                plain = await session.stream_scalars(select(ContractParent))
                assert plain._unique_filter_state is None
                await plain.close()
        finally:
            await engine.dispose()
