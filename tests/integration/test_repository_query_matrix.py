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
"""Reads: paging, ordering, fetch plans, locks, name validation and streams, on every lane.

- Every paging path orders by the primary key after the requested orders, so pages are deterministic and
  an unsorted page compiles on SQL Server, which requires an ORDER BY for OFFSET (C055).
- ``find_all(Pageable)`` skips the ``COUNT`` when the page proves the total; ``find_slice`` never counts
  (``LIMIT size + 1``); ``scroll`` pages by keyset (C135).
- A specification whose join repeats entities (a collection) is paged over the distinct keys, and one that
  joins a many-to-one with a plain ``LIMIT``; either way its own fetch plan, loader criteria, execution
  options and lock apply to the entities it reads and to its count.
- NULL placement and case folding come out the same on every backend when an order names them (C112).
- A ``lazy="joined"`` collection works in every list method and in ``stream_all`` (C140).
- Read methods take a fetch plan, so relationships are usable on the detached entities they return, and
  ``find_by_id`` takes a pessimistic lock that needs a read-write transaction (C137).
- Sort and filter names are validated, with allow-lists for hidden columns (C141).
- ``stream_all`` reads in batches (C136), optionally of a fixed size; a fetch plan that joins a collection
  streams whole entities.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String, event, insert
from sqlalchemy.dialects import mssql, oracle
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, Session, joinedload, mapped_column, relationship, selectinload, with_loader_criteria

from pyfly.data import transactional
from pyfly.data.page import Page
from pyfly.data.pageable import KeysetPosition, Order, Pageable, Sort
from pyfly.data.property_resolver import InvalidPropertyError
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.relational.sqlalchemy.statements import LockMode
from pyfly.data.transaction import IllegalTransactionStateError, Propagation
from tests.integration._repository_harness import Datasources, dml, repository_datasources, sql_of
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import CONTRACT_MODELS, ContractChild, ContractParent


class RqScore(Base):
    __tablename__ = "rq_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    secret: Mapped[str] = mapped_column(String(40), default="hidden")

    @property
    def display_name(self) -> str:
        return (self.name or "").title()


class RqShelf(Base):
    """A shelf whose books load eagerly with a join (a ``lazy="joined"`` collection)."""

    __tablename__ = "rq_shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(40))
    books: Mapped[list[RqBook]] = relationship(lazy="joined", order_by="RqBook.id")


class RqBook(Base):
    __tablename__ = "rq_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("rq_shelf.id"))


class RqEntry(Base):
    """An entry whose score comes with it through a join (a ``lazy="joined"`` many-to-one)."""

    __tablename__ = "rq_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    score_id: Mapped[int] = mapped_column(ForeignKey("rq_score.id"))
    score: Mapped[RqScore] = relationship(lazy="joined")


class RqOwner(Base):
    __tablename__ = "rq_owner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    country: Mapped[str] = mapped_column(String(10))


class RqShop(Base):
    """A shop with an owner (a many-to-one) and items (a collection), both loaded lazily unless a plan says."""

    __tablename__ = "rq_shop"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[int] = mapped_column(ForeignKey("rq_owner.id"))
    owner: Mapped[RqOwner] = relationship()
    items: Mapped[list[RqItem]] = relationship(order_by="RqItem.id")


class RqItem(Base):
    __tablename__ = "rq_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    shop_id: Mapped[int] = mapped_column(ForeignKey("rq_shop.id"))
    label: Mapped[str] = mapped_column(String(40))


class EntryRepository(Repository[RqEntry, int]):
    pass


class ShopRepository(Repository[RqShop, int]):
    pass


class ScoreRepository(Repository[RqScore, int]):
    pass


class GuardedScoreRepository(Repository[RqScore, int]):
    __sortable__ = ("name", "score")
    __filterable__ = ("name",)


class ShelfRepository(Repository[RqShelf, int]):
    pass


class ParentRepository(Repository[ContractParent, uuid.UUID]):
    pass


class EagerParentRepository(Repository[ContractParent, uuid.UUID]):
    __load__ = ("children",)


MODELS = (*CONTRACT_MODELS, RqScore, RqShelf, RqBook, RqEntry, RqOwner, RqShop, RqItem)

SCORES = {1: 10, 2: None, 3: 5, 4: None, 5: 7, 6: 3, 7: 1}
"""The audit's probe rows (C112): id -> score, two of them NULL."""


async def _scores(datasources: Datasources, names: dict[int, str | None] | None = None) -> None:
    rows = [{"id": key, "score": score, "name": (names or {}).get(key, "same")} for key, score in SCORES.items()]
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(RqScore), rows)


async def _shelves(datasources: Datasources, count: int, books: int = 3) -> None:
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(RqShelf), [{"id": n, "name": f"s{n}"} for n in range(1, count + 1)])
        rows = [{"id": n * 100 + b, "shelf_id": n} for n in range(1, count + 1) for b in range(books)]
        await conn.execute(insert(RqBook), rows)


async def _families(parents: ParentRepository, count: int) -> list[ContractParent]:
    families = []
    for n in range(count):
        parent = ContractParent(name=f"p{n}")
        parent.children.extend(ContractChild(label=f"c{n}.{k}", position=k) for k in range(2))
        families.append(parent)
    return await parents.save_all(families)


def _ids(items: list[Any]) -> list[int]:
    return [item.id for item in items]


# ---------------------------------------------------------------------------------------------------------
# WP03-05: deterministic paging
# ---------------------------------------------------------------------------------------------------------


async def test_pages_over_tied_sort_keys_cover_every_row_once(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)  # every row has the name "same"
        scores = ScoreRepository()
        for sort in (Sort.by("name"), Sort.unsorted()):
            seen: list[int] = []
            for page in (1, 2, 3):
                seen += _ids((await scores.find_all(Pageable.of(page, 3, sort))).items)
            assert seen == [1, 2, 3, 4, 5, 6, 7]  # ordered by the key after the ties: stable, complete


async def test_every_paging_statement_compiles_on_sql_server(relational_backend: RelationalBackend) -> None:
    """The paging statements the repository really ran, compiled for SQL Server, which rejects OFFSET without
    ORDER BY (every page, the first one and an unsorted one included, raised CompileError there)."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        ran: list[Any] = []

        def record(state: Any) -> None:
            if state.is_select:
                ran.append(state.statement)

        scores = ScoreRepository()
        every = Specification[RqScore](lambda root, q: q.where(root.id > 0))
        with _listening(Session, "do_orm_execute", record):
            await scores.find_all(Pageable.of(1, 3))
            await scores.find_all(Pageable.of(2, 3, Sort.by(Order.desc("score"))))
            await scores.find_all_by_spec_paged(every, Pageable.of(1, 2))
            await scores.find_slice(Pageable.of(1, 2))
            await scores.find_slice_by_spec(every, Pageable.of(2, 2))
            await scores.scroll(Sort.by("name"), size=2)
        paged = [statement for statement in ran if statement._limit_clause is not None]
        assert len(paged) == 6
        for statement in paged:
            sql = " ".join(str(statement.compile(dialect=mssql.dialect())).split())
            assert "ORDER BY" in sql and "rq_score.id ASC" in sql


@contextlib.contextmanager
def _listening(target: Any, name: str, listener: Any) -> Any:
    event.listen(target, name, listener)
    try:
        yield
    finally:
        event.remove(target, name, listener)


# ---------------------------------------------------------------------------------------------------------
# WP03-10: count skipping, slices, keyset scrolling
# ---------------------------------------------------------------------------------------------------------


async def test_a_page_counts_only_when_its_content_cannot_tell(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()

        async def page_of(pageable: Pageable) -> tuple[Page[RqScore], dict[str, int]]:
            with datasources.counter() as counter:
                page = await scores.find_all(pageable)
            return page, dml(counter)

        short_first, counts = await page_of(Pageable.of(1, 10))
        assert (short_first.total, counts) == (7, {"SELECT": 1})
        full_first, counts = await page_of(Pageable.of(1, 3))
        assert (full_first.total, counts) == (7, {"SELECT": 2})
        short_last, counts = await page_of(Pageable.of(3, 3))
        assert (short_last.total, _ids(short_last.items), counts) == (7, [7], {"SELECT": 1})
        beyond, counts = await page_of(Pageable.of(9, 3))
        assert (beyond.total, beyond.items, counts) == (7, [], {"SELECT": 2})
        everything, counts = await page_of(Pageable.unpaged())
        assert (everything.total, counts) == (7, {"SELECT": 1})


async def test_a_slice_needs_no_count(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()
        with datasources.counter() as counter:
            first = await scores.find_slice(Pageable.of(1, 3, Sort.by("id")), name="same")
            last = await scores.find_slice(Pageable.of(3, 3, Sort.by("id")))
        assert (_ids(first.items), first.has_next) == ([1, 2, 3], True)
        assert (_ids(last.items), last.has_next, last.has_previous) == ([7], False, True)
        assert dml(counter) == {"SELECT": 2}
        assert "count(" not in " ".join(sql_of(counter, "SELECT")).lower()


async def test_a_keyset_scroll_visits_every_row_once_in_order(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources, names={1: "b", 2: "a", 3: "b", 4: "c", 5: "a", 6: "b", 7: "c"})
        scores = ScoreRepository()
        for sort, expected in (
            (Sort.by("name"), [2, 5, 1, 3, 6, 4, 7]),
            (Sort.by(Order.desc("name")), [4, 7, 1, 3, 6, 2, 5]),
        ):
            seen: list[int] = []
            position: KeysetPosition | None = None
            while True:
                window = await scores.scroll(sort, position, size=3)
                seen += _ids(window.items)
                if not window.has_next:
                    break
                position = window.next_position
            assert seen == expected

        only_b = Specification[RqScore](lambda root, q: q.where(root.name == "b"))
        window = await scores.scroll(Sort.by("name"), KeysetPosition.of(name="b", id=1), size=5, spec=only_b)
        assert (_ids(window.items), window.has_next) == ([3, 6], False)
        with pytest.raises(ValueError, match="lacks the key"):
            await scores.scroll(Sort.by("name"), KeysetPosition.of(name="b"))
        with pytest.raises(ValueError, match="null handling"):
            await scores.scroll(Sort.by(Order.asc("name").nulls_last()))


HAS_A_HIT = Specification[ContractParent](
    lambda root, q: q.join(ContractParent.children).where(ContractChild.label == "hit")
)
"""A specification that joins a collection: the join repeats a parent once per matching child."""


async def _hits(parents: ParentRepository) -> None:
    """Parents a, b and c with 3, 2 and 1 children labeled 'hit', and d with none; each has one 'miss'."""
    families = []
    for name, hits in (("a", 3), ("b", 2), ("c", 1), ("d", 0)):
        parent = ContractParent(name=name)
        parent.children.extend(ContractChild(label="hit", position=k) for k in range(hits))
        parent.children.append(ContractChild(label="miss", position=9))
        families.append(parent)
    await parents.save_all(families)


def _names(items: list[ContractParent]) -> list[str]:
    return [item.name for item in items]


async def test_a_specification_that_joins_a_collection_pages_each_entity_once(
    relational_backend: RelationalBackend,
) -> None:
    """Pages, slices and windows count entities, not joined rows: cut on joined rows, the first page of three
    held only 'a' (its three rows), its total said 1, and paging never reached 'b' or 'c'."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        await _hits(parents)
        by_name = Sort.by("name")

        with datasources.counter() as counter:
            first = await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.of(1, 2, by_name), load="children")
        assert (_names(first.items), first.total, first.total_pages) == (["a", "b"], 3, 2)
        assert [len(parent.children) for parent in first.items] == [4, 3]
        assert dml(counter) == {"SELECT": 3}  # the page, its children, and the COUNT of distinct parents
        last = await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.of(2, 2, by_name))
        assert (_names(last.items), last.total) == (["c"], 3)
        with datasources.counter() as counter:
            whole = await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.of(1, 3, Sort.by(Order.desc("name"))))
        assert (_names(whole.items), whole.total, dml(counter)) == (["c", "b", "a"], 3, {"SELECT": 2})
        folded = Sort.by(Order.desc("name").ignoring_case().nulls_last())
        assert _names((await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.of(1, 2, folded))).items) == ["c", "b"]
        unpaged = await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.unpaged())
        assert (sorted(_names(unpaged.items)), unpaged.total) == (["a", "b", "c"], 3)

        head = await parents.find_slice_by_spec(HAS_A_HIT, Pageable.of(1, 2, by_name))
        assert (_names(head.items), head.has_next) == (["a", "b"], True)
        tail = await parents.find_slice_by_spec(HAS_A_HIT, Pageable.of(2, 2, by_name))
        assert (_names(tail.items), tail.has_next) == (["c"], False)

        window = await parents.scroll(by_name, size=2, spec=HAS_A_HIT)
        assert (_names(window.items), window.has_next) == (["a", "b"], True)
        window = await parents.scroll(by_name, window.next_position, size=2, spec=HAS_A_HIT)
        assert (_names(window.items), window.has_next) == (["c"], False)

        assert sorted(_names(await parents.find_all_by_spec(HAS_A_HIT))) == ["a", "b", "c"]


async def test_distinct_entity_pages_compile_on_sql_server_and_oracle(relational_backend: RelationalBackend) -> None:
    """The statements a joining specification's page, slice and window really ran, compiled for SQL Server
    2012+ and Oracle: the page is taken over the distinct keys, with the ORDER BY those need for OFFSET."""
    async with repository_datasources(relational_backend, *MODELS):
        parents = ParentRepository()
        await _hits(parents)
        ran: list[Any] = []

        def record(state: Any) -> None:
            if state.is_select:
                ran.append(state.statement)

        with _listening(Session, "do_orm_execute", record):
            await parents.find_all_by_spec_paged(HAS_A_HIT, Pageable.of(2, 1, Sort.by(Order.desc("name"))))
            await parents.find_slice_by_spec(HAS_A_HIT, Pageable.of(1, 2))
            await parents.scroll(Sort.by("name"), size=2, spec=HAS_A_HIT)
        server = mssql.dialect()
        server._supports_offset_fetch = True  # what its first connection to SQL Server 2012 or later sets
        for dialect in (server, oracle.dialect()):
            compiled = [" ".join(str(statement.compile(dialect=dialect)).split()) for statement in ran]
            paged = [sql for sql in compiled if "SELECT DISTINCT" in sql and " JOIN (" in sql]
            assert len(paged) == 3, compiled
            assert all(("FETCH" in sql or "SELECT DISTINCT TOP" in sql) and " ORDER BY " in sql for sql in paged)


async def _shops(datasources: Datasources) -> None:
    """Owners 1 (ES) and 2 (FR); shops s1..s6, the odd ones Spanish; items 'hit', 'hit', 'miss' in each."""
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(RqOwner), [{"id": 1, "country": "ES"}, {"id": 2, "country": "FR"}])
        await conn.execute(insert(RqShop), [{"id": n, "name": f"s{n}", "owner_id": 2 - n % 2} for n in range(1, 7)])
        labels = ("hit", "hit", "miss")
        rows = [{"id": n * 10 + k, "shop_id": n, "label": label} for n in range(1, 7) for k, label in enumerate(labels)]
        await conn.execute(insert(RqItem), rows)


def _spanish_shops(through: str) -> Specification[RqShop]:
    """The Spanish shops, found through a join to their owner (a many-to-one: one row per shop) or to their
    items (a collection: two 'hit' rows per shop), with a fetch plan and an execution option of their own."""

    def predicate(root: Any, query: Any) -> Any:
        if through == "owner":
            query = query.join(RqShop.owner).where(RqOwner.country == "ES")
        else:
            query = query.join(RqShop.items).where(RqItem.label == "hit", RqShop.owner_id == 1)
        plan = (selectinload(RqShop.items), joinedload(RqShop.owner))
        return query.options(*plan).execution_options(pyfly_probe="spec")

    return Specification[RqShop](predicate)


@pytest.mark.parametrize("through", ["owner", "items"])
async def test_a_specification_keeps_its_fetch_plan_and_options_on_every_paging_path(
    relational_backend: RelationalBackend, through: str
) -> None:
    """A specification's own loader options and execution options apply to the statement that reads the
    entities, on every path: when its join repeats entities, the page is cut from the distinct keys by another
    statement, which dropped them (the detached shops' items raised DetachedInstanceError)."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _shops(datasources)
        shops = ShopRepository()
        spec = _spanish_shops(through)
        by_name = Sort.by("name")
        options: list[Any] = []

        def record(state: Any) -> None:
            if state.is_select and not state.is_relationship_load:
                options.append((str(state.statement), state.execution_options.get("pyfly_probe")))

        with _listening(Session, "do_orm_execute", record):
            page = await shops.find_all_by_spec_paged(spec, Pageable.of(1, 2, by_name))
            tail = await shops.find_slice_by_spec(spec, Pageable.of(2, 2, by_name))
            window = await shops.scroll(by_name, size=2, spec=spec)
            listed = await shops.find_all_by_spec(spec)
        assert (_names(page.items), page.total) == (["s1", "s3"], 3)
        assert (_names(tail.items), tail.has_next) == (["s5"], False)
        assert (_names(window.items), window.has_next) == (["s1", "s3"], True)
        assert sorted(_names(listed)) == ["s1", "s3", "s5"]
        for shop in [*page.items, *tail.items, *window.items, *listed]:
            assert [item.label for item in shop.items] == ["hit", "hit", "miss"]
            assert shop.owner.country == "ES"
        reads = [(sql, probe) for sql, probe in options if "count(" not in sql.lower()]
        assert len(reads) == 4 and {probe for _sql, probe in reads} == {"spec"}
        # A many-to-one repeats no shop: its pages are cut by a plain LIMIT, and only the collection needs the keys.
        paged_by_keys = ["DISTINCT" in sql for sql, _probe in reads]
        assert paged_by_keys == ([False] * 4 if through == "owner" else [True, True, True, False])


@pytest.mark.parametrize("through", ["owner", "items"])
async def test_a_specification_s_loader_criteria_apply_to_its_page_and_its_count(
    relational_backend: RelationalBackend, through: str
) -> None:
    """``with_loader_criteria`` in a specification narrows the entities it pages and the total it counts alike:
    the COUNT ran over a subquery, where the ORM applies no option, and counted the shops the criteria hide."""

    def predicate(root: Any, query: Any) -> Any:
        if through == "owner":
            query = query.join(RqShop.owner).where(RqOwner.country.in_(["ES", "FR"]))
        else:
            query = query.join(RqShop.items).where(RqItem.label == "hit")
        return query.options(with_loader_criteria(RqShop, RqShop.name != "s1"))

    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _shops(datasources)
        shops = ShopRepository()
        page = await shops.find_all_by_spec_paged(Specification[RqShop](predicate), Pageable.of(1, 2, Sort.by("name")))
        assert (_names(page.items), page.total) == (["s2", "s3"], 5)


@pytest.mark.parametrize("of", [None, RqShop], ids=["every-table", "of-the-entity"])
async def test_a_locking_specification_pages_over_distinct_entities(
    relational_backend: RelationalBackend, of: Any
) -> None:
    """A specification that locks (``with_for_update``) and joins a collection is paged over the distinct keys:
    the lock goes on the statement that reads the entities, not on the ``DISTINCT`` keys (which PostgreSQL
    refuses to lock), and holds the entities' rows until the transaction ends."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _shops(datasources)
        shops = ShopRepository()
        locking = Specification[RqShop](
            lambda root, q: (
                q.join(RqShop.items).where(RqItem.label == "hit", RqShop.owner_id == 1).with_for_update(of=of)
            )
        )

        row_locks = datasources.dialect != "sqlite"  # SQLite has no row locks: its writer holds the database

        @transactional(propagation=Propagation.REQUIRES_NEW)
        async def try_nowait(id: int) -> None:
            await shops.find_by_id(id, lock=LockMode.PESSIMISTIC_WRITE_NOWAIT)

        @transactional
        async def lock_page() -> tuple[list[str], list[str]]:
            with datasources.counter() as counter:
                page = await shops.find_all_by_spec_paged(locking, Pageable.of(1, 2, Sort.by("name")))
            if row_locks:
                with pytest.raises(DBAPIError):  # another transaction cannot take a row of the page
                    await try_nowait(1)
            return _names(page.items), sql_of(counter, "SELECT")

        names, statements = await lock_page()
        assert names == ["s1", "s3"]
        (read,) = [sql for sql in statements if "pyfly_page" in sql]
        keys = read.split(") AS pyfly_page")[0]
        assert "FOR UPDATE" not in keys and "FOR SHARE" not in keys
        assert not any("FOR UPDATE" in sql for sql in statements if "count(" in sql.lower())  # a COUNT locks nothing
        if row_locks:
            assert re.search(r"FOR UPDATE( OF [`\"]?rq_shop[`\"]?)?\s*$", read), read


# ---------------------------------------------------------------------------------------------------------
# WP03-17: NULL placement and case, the same everywhere
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (Order.asc("score").nulls_last(), [7, 6, 3, 5, 1, 2, 4]),
        (Order.asc("score").nulls_first(), [2, 4, 7, 6, 3, 5, 1]),
        (Order.desc("score").nulls_last(), [1, 5, 3, 6, 7, 2, 4]),
        (Order.desc("score").nulls_first(), [2, 4, 1, 5, 3, 6, 7]),
    ],
    ids=["asc-last", "asc-first", "desc-last", "desc-first"],
)
async def test_null_placement_is_portable(
    relational_backend: RelationalBackend, order: Order, expected: list[int]
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()
        assert _ids((await scores.find_all(Pageable.of(1, 10, Sort.by(order)))).items) == expected
        assert _ids((await scores.find_slice(Pageable.of(1, 10, Sort.by(order)))).items) == expected


async def test_ignore_case_orders_by_the_folded_value(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources, names={1: "Bob", 2: "alice", 3: "Carl", 4: "dave", 5: "Eve", 6: "frank", 7: "Gus"})
        ordered = await ScoreRepository().find_all(Sort.by(Order.asc("name").ignoring_case()))
        assert [row.name for row in ordered] == ["alice", "Bob", "Carl", "dave", "Eve", "frank", "Gus"]
        # Only strings fold (as Spring's QueryUtils): lower() of an integer does not exist on PostgreSQL.
        numbers = await ScoreRepository().find_all(Sort.by(Order.desc("id").ignoring_case()))
        assert _ids(numbers) == [7, 6, 5, 4, 3, 2, 1]


# ---------------------------------------------------------------------------------------------------------
# WP03-15: joined collections
# ---------------------------------------------------------------------------------------------------------


async def test_a_joined_collection_works_in_every_list_method(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _shelves(datasources, 4)
        shelves = ShelfRepository()
        every = Specification[RqShelf](lambda root, q: q.where(root.id > 0))

        assert [len(shelf.books) for shelf in await shelves.find_all()] == [3, 3, 3, 3]
        assert _ids(await shelves.find_all(Sort.by(Order.desc("id")))) == [4, 3, 2, 1]
        page = await shelves.find_all(Pageable.of(1, 3))
        assert (_ids(page.items), page.total) == ([1, 2, 3], 4)  # the LIMIT counts shelves, not joined rows
        assert all(len(shelf.books) == 3 for shelf in page.items)
        assert sorted(_ids(await shelves.find_all_by_id([2, 3]))) == [2, 3]
        assert len(await shelves.find_all_by_spec(every)) == 4
        assert (await shelves.find_all_by_spec_paged(every, Pageable.of(2, 3))).total == 4
        assert _ids((await shelves.find_slice(Pageable.of(1, 2))).items) == [1, 2]
        assert _ids((await shelves.scroll(Sort.by("name"), size=2)).items) == [1, 2]
        streamed = [shelf async for shelf in shelves.stream_all(Sort.by("id"))]
        assert [(shelf.id, len(shelf.books)) for shelf in streamed] == [(1, 3), (2, 3), (3, 3), (4, 3)]


# ---------------------------------------------------------------------------------------------------------
# WP03-12: fetch plans and locks
# ---------------------------------------------------------------------------------------------------------


async def test_a_fetch_plan_loads_relationships_the_detached_entity_can_use(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS):
        parents = ParentRepository()
        first, second = await _families(parents, 2)

        found = await parents.find_by_id(first.id, load="children")
        assert found is not None and [child.label for child in found.children] == ["c0.0", "c0.1"]
        listed = await parents.find_all(Sort.by("name"), load=[ContractParent.children])
        assert [len(parent.children) for parent in listed] == [2, 2]
        paged = await parents.find_all(Pageable.of(1, 1, Sort.by("name")), load="children")
        assert len(paged.items[0].children) == 2
        by_id = await parents.find_all_by_id([second.id], load="children")
        assert len(by_id[0].children) == 2
        streamed = [parent async for parent in parents.stream_all(Sort.by("name"), load="children")]
        assert [len(parent.children) for parent in streamed] == [2, 2]
        child = await ChildRepositoryForPaths().find_all(load="parent.children")
        assert {len(row.parent.children) for row in child} == {2}

        defaulted = await EagerParentRepository().find_by_id(second.id)
        assert defaulted is not None and len(defaulted.children) == 2
        with pytest.raises(InvalidPropertyError):
            await parents.find_by_id(first.id, load="name")


class ChildRepositoryForPaths(Repository[ContractChild, int]):
    pass


async def test_a_lock_is_taken_inside_a_transaction_and_refused_outside(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()

        @transactional
        async def lock_row() -> tuple[int | None, list[str]]:
            with datasources.counter() as counter:
                row = await scores.find_by_id(3, lock=LockMode.PESSIMISTIC_WRITE)
            return (row.score if row is not None else None), sql_of(counter, "SELECT")

        score, statements = await lock_row()
        assert score == 5
        if datasources.dialect != "sqlite":  # SQLite has no row locks: its writer holds the database
            assert statements[-1].rstrip().endswith("FOR UPDATE")
        with pytest.raises(IllegalTransactionStateError, match="read-write transaction"):
            await scores.find_by_id(3, lock=LockMode.PESSIMISTIC_WRITE)

        @transactional(read_only=True)
        async def lock_in_a_read_only_unit() -> None:
            await scores.find_by_id(3, lock=LockMode.PESSIMISTIC_READ)

        with pytest.raises(IllegalTransactionStateError):
            await lock_in_a_read_only_unit()


@pytest.mark.backends("pg", "mysql", "mariadb")
async def test_a_nowait_lock_fails_at_once_on_a_row_another_unit_holds(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()
        held = asyncio.Event()
        release = asyncio.Event()

        @transactional
        async def hold() -> None:
            await scores.find_by_id(3, lock=LockMode.PESSIMISTIC_WRITE)
            held.set()
            await release.wait()

        @transactional(propagation=Propagation.REQUIRES_NEW)
        async def try_nowait() -> None:
            await scores.find_by_id(3, lock=LockMode.PESSIMISTIC_WRITE_NOWAIT)

        holder = asyncio.create_task(hold())
        await held.wait()
        try:
            with pytest.raises(DBAPIError):
                await asyncio.wait_for(try_nowait(), timeout=10)
        finally:
            release.set()
            await holder
        assert datasources.checked_out() == 0


# ---------------------------------------------------------------------------------------------------------
# WP03-16: validated names
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends("sqlite-file")
async def test_sort_and_filter_names_are_validated(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()
        for bad in ("display_name", "nope", "__table__", "$where"):
            with pytest.raises(InvalidPropertyError):
                await scores.find_all(Sort.by(bad))
            with pytest.raises(InvalidPropertyError):
                await scores.find_all(Pageable.of(1, 2, Sort.by(bad)))
        with pytest.raises(InvalidPropertyError):
            await ParentRepository().find_all(Sort.by("children"))  # a relationship
        with pytest.raises(InvalidPropertyError):
            await scores.find_all(nope=1)
        assert len(await scores.find_all(Sort.by("secret"), secret="hidden")) == 7  # no allow-list: every column

        guarded = GuardedScoreRepository()
        with pytest.raises(InvalidPropertyError, match="secret"):
            await guarded.find_all(Sort.by("secret"))
        with pytest.raises(InvalidPropertyError, match="secret"):
            await guarded.find_all(secret="hidden")  # an equality oracle on a hidden column
        with pytest.raises(InvalidPropertyError):
            await guarded.find_all(score=5)  # sortable, but not filterable
        assert _ids(await guarded.find_all(Sort.by(Order.desc("score").nulls_last()), name="same"))[:2] == [1, 5]


# ---------------------------------------------------------------------------------------------------------
# WP03-11: streams read in batches
# ---------------------------------------------------------------------------------------------------------


async def test_stream_all_reads_fixed_size_batches_when_asked(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        scores = ScoreRepository()
        assert [row.id async for row in scores.stream_all(Sort.by("id"), chunk_size=2)] == [1, 2, 3, 4, 5, 6, 7]
        assert [row.id async for row in scores.stream_all(Sort.by("id"), score=5)] == [3]
        with pytest.raises(ValueError, match="chunk_size"):
            async with contextlib.aclosing(scores.stream_all(chunk_size=0)) as rows:
                async for _row in rows:
                    pass
        assert datasources.checked_out() == 0


async def test_a_stream_whose_fetch_plan_joins_a_collection_yields_whole_entities(
    relational_backend: RelationalBackend,
) -> None:
    """A joined eager load of a collection spreads one entity over several rows, which only make it whole read
    together: such a stream reads its result in full, on every backend, instead of failing on the first batch
    (PostgreSQL and SQLite raised 'unique() must be invoked')."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        await _families(parents, 3)
        plan = joinedload(ContractParent.children)
        for chunk_size in (None, 1):
            streamed = [
                parent async for parent in parents.stream_all(Sort.by("name"), load=plan, chunk_size=chunk_size)
            ]
            assert [(parent.name, len(parent.children)) for parent in streamed] == [("p0", 2), ("p1", 2), ("p2", 2)]
        async with contextlib.aclosing(parents.stream_all(Sort.by("name"), load=plan)) as rows:
            first = await anext(rows)
        assert [child.label for child in first.children] == ["c0.0", "c0.1"]
        assert datasources.checked_out() == 0


@pytest.mark.backends("mysql", "mariadb")
async def test_a_joined_many_to_one_is_streamed_from_the_cursor_on_mysql(relational_backend: RelationalBackend) -> None:
    """A joined many-to-one comes with its row and needs no statement per batch, so MySQL and MariaDB stream it
    from the server-side cursor instead of reading everything first (which only per-batch loads require): the
    open cursor is why another statement on the unit is refused until the stream is closed."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _scores(datasources)
        async with datasources.engine.begin() as conn:
            await conn.execute(insert(RqEntry), [{"id": n, "score_id": n} for n in SCORES])
        entries = EntryRepository()

        @transactional
        async def peek() -> tuple[int, int]:
            async with contextlib.aclosing(entries.stream_all(Sort.by("id"))) as rows:
                first = await anext(rows)
                with pytest.raises(IllegalTransactionStateError):
                    await entries.count()
            return first.score.id, await entries.count()

        assert await peek() == (1, 7)
        assert [(entry.id, entry.score.id) async for entry in entries.stream_all(Sort.by("id"))][-1] == (7, 7)
