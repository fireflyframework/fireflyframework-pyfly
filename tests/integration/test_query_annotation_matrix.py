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
"""``@query`` methods on every lane.

- Entity results are the unit of work's own entities (C048, C050): identity-mapped, typed by their columns (a
  UUID is a ``UUID`` and a boolean a ``bool`` on SQLite too), mapped by attribute whatever the column is
  called, with their relationships loaded, and the unit's pending changes flushed first; saving one updates it.
- The JPQL transpiler works on tokens (C049, C051): boolean literals are what the dialect accepts, an alias is
  rewritten only where it is one (never inside a string literal or another identifier), a correlated subquery
  keeps its correlation, the one of a modifying statement over its own table included (only the target loses
  its alias), and a colon in a literal or a comment is never a parameter, escaped with ``\\:`` or not.
- The result is shaped by the return annotation, not guessed from the SQL (C127): a list query with an
  ``EXISTS`` subquery returns a list, scalars, rows and projections come back as declared, ``@modifying``
  statements return their row count (after a ``WITH`` clause too), a statement that is not a plain ``SELECT``
  runs in a write unit, and arguments bind by position or keyword (``?1`` in JPQL too).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import pytest
from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.data import transactional
from pyfly.data.projection import projection
from pyfly.data.query import modifying, query
from pyfly.data.query_parser import IncorrectResultSizeException
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.types import UtcDateTime
from pyfly.kernel.exceptions import DataIntegrityException
from tests.integration._repository_harness import Datasources, repository_datasources
from tests.support.backend_matrix import RelationalBackend


class QaAuthor(Base):
    __tablename__ = "qa_author"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    display_name: Mapped[str] = mapped_column("dname", String(40), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(40), nullable=True)
    region: Mapped[str] = mapped_column(String(20))
    token: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    joined_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=lambda: datetime.now(UTC))
    books: Mapped[list[QaBook]] = relationship(lazy="selectin", order_by="QaBook.id")


class QaBook(Base):
    __tablename__ = "qa_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("qa_author.id"))
    title: Mapped[str] = mapped_column(String(40))
    author: Mapped[QaAuthor] = relationship(lazy="joined", overlaps="books")


class QaShelf(Base):
    """A shelf whose books load eagerly with a join (a ``lazy="joined"`` collection)."""

    __tablename__ = "qa_shelf"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    books: Mapped[list[QaShelfBook]] = relationship(lazy="joined", order_by="QaShelfBook.id")


class QaShelfBook(Base):
    __tablename__ = "qa_shelf_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    shelf_id: Mapped[int] = mapped_column(ForeignKey("qa_shelf.id"))


class QaCrate(Base):
    """A crate whose items load eagerly with a subquery (a ``lazy="subquery"`` collection)."""

    __tablename__ = "qa_crate"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    items: Mapped[list[QaCrateItem]] = relationship(lazy="subquery", order_by="QaCrateItem.id")


class QaCrateItem(Base):
    __tablename__ = "qa_crate_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    crate_id: Mapped[int] = mapped_column(ForeignKey("qa_crate.id"))


class QaNode(Base):
    """A tree: a node's parent is another node of the same table."""

    __tablename__ = "qa_node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaf: Mapped[bool] = mapped_column(Boolean, default=False)


class NodeRepository(Repository[QaNode, int]):
    @modifying
    @query(
        "DELETE FROM QaNode n WHERE n.parent_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM QaNode c WHERE c.parent_id = n.id)"
    )
    async def prune_leaves(self) -> int: ...

    @modifying
    @query("UPDATE QaNode n SET n.leaf = true WHERE NOT EXISTS (SELECT 1 FROM QaNode c WHERE c.parent_id = n.id)")
    async def mark_leaves(self) -> int: ...

    @modifying
    @query("DELETE FROM QaNode n WHERE n.id IN (SELECT n.parent_id FROM QaNode n WHERE n.id > :floor)")
    async def delete_parents_of_nodes_above(self, floor: int) -> int: ...


class CrateRepository(Repository[QaCrate, int]):
    @query("SELECT c FROM QaCrate c ORDER BY c.id")
    async def every(self) -> list[QaCrate]: ...


class BookRepository(Repository[QaBook, int]):
    @query("SELECT b FROM QaBook b WHERE b.title LIKE :prefix ORDER BY b.id")
    async def titled(self, prefix: str) -> list[QaBook]: ...


class ShelfRepository(Repository[QaShelf, int]):
    @query("SELECT * FROM qa_shelf ORDER BY id", native=True)
    async def every(self) -> list[QaShelf]: ...


@projection
class AuthorName(Protocol):
    id: int
    display_name: str


class AuthorRepository(Repository[QaAuthor, int]):
    @query("SELECT a FROM QaAuthor a WHERE a.active = true ORDER BY a.id")
    async def find_active(self) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE a.active = false OR a.note = 'status = true'")
    async def find_inactive_or_odd(self) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE a.note = 'a.n/a.' AND a.region = :region")
    async def find_noted(self, region: str) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE EXISTS (SELECT 1 FROM qa_book b WHERE b.author_id = a.id) ORDER BY a.id")
    async def find_with_books(self) -> list[QaAuthor]: ...

    @query("SELECT COUNT(a) FROM QaAuthor a WHERE EXISTS (SELECT 1 FROM qa_book b WHERE b.author_id = a.id)")
    async def count_with_books(self) -> int: ...

    @query("SELECT a FROM QaAuthor a WHERE NOT EXISTS (SELECT 1 FROM qa_book b WHERE b.author_id = a.id)")
    async def find_without_books(self) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE a.display_name = :name")
    async def find_named(self, name: str) -> QaAuthor | None: ...

    @query("SELECT a FROM QaAuthor a WHERE a.region = ?1 AND a.active = ?2 ORDER BY a.id")
    async def find_in_region(self, region: str, active: bool) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE a.region = :region")
    async def find_one_in_region(self, region: str) -> QaAuthor | None: ...

    @query("SELECT a FROM QaAuthor a WHERE a.id IN (:ids) ORDER BY a.id")
    async def find_ids(self, ids: list[int]) -> list[QaAuthor]: ...

    @query("SELECT a FROM QaAuthor a WHERE a.token = :token")
    async def find_by_token_query(self, token: uuid.UUID) -> QaAuthor | None: ...

    @query("SELECT a.display_name FROM QaAuthor a WHERE a.region = :region ORDER BY a.id")
    async def names_in(self, region: str) -> list[str]: ...

    @query("SELECT a.active FROM QaAuthor a WHERE a.id = :id")
    async def is_active(self, id: int) -> bool | None: ...

    @query("SELECT a.id, a.display_name FROM QaAuthor a ORDER BY a.id")
    async def pairs(self) -> list[tuple[int, str]]: ...

    @query("SELECT a.id AS id, a.display_name AS display_name FROM QaAuthor a WHERE a.region = :region ORDER BY a.id")
    async def views(self, region: str) -> list[AuthorName]: ...

    @query("SELECT dname, region FROM qa_author WHERE id = :id", native=True)
    async def row_of(self, id: int) -> dict[str, Any] | None: ...

    @query("SELECT * FROM qa_author WHERE region = :region ORDER BY id", native=True)
    async def native_in_region(self, region: str) -> list[QaAuthor]: ...

    @query("SELECT EXISTS (SELECT 1 FROM qa_book WHERE title = :title)", native=True)
    async def has_title(self, title: str) -> bool: ...

    @modifying
    @query("UPDATE QaAuthor a SET a.active = false, a.note = :note WHERE a.region = :region")
    async def deactivate(self, region: str, note: str) -> int: ...

    @modifying(clear_automatically=True)
    @query(
        "DELETE FROM QaAuthor a WHERE a.region = :region "
        "AND NOT EXISTS (SELECT 1 FROM qa_book b WHERE b.author_id = a.id)"
    )
    async def purge(self, region: str) -> int: ...

    @query("UPDATE qa_author SET region = :region WHERE id = :id", native=True)
    @modifying
    async def move(self, id: int, region: str) -> None: ...

    @modifying
    @query("UPDATE QaAuthor a SET a.display_name = :name WHERE a.id = :id")
    async def rename(self, id: int, name: str) -> int: ...

    @modifying
    @query(
        "WITH titled AS (SELECT author_id FROM qa_book WHERE title = :title) "
        "UPDATE qa_author SET note = :note WHERE id IN (SELECT author_id FROM titled)",
        native=True,
    )
    async def note_authors_of(self, title: str, note: str) -> int: ...

    @query("SELECT a.id FROM QaAuthor a WHERE a.note = '10:30'")
    async def noted_at_half_past(self) -> list[int]: ...

    @query("SELECT a.id FROM QaAuthor a WHERE a.note = '10\\:30'")
    async def noted_at_half_past_escaped(self) -> list[int]: ...

    @query("SELECT id FROM qa_author WHERE note = '10:30'", native=True)
    async def native_noted_at_half_past(self) -> list[int]: ...

    @query("SELECT id FROM qa_author WHERE note = '10\\:30'", native=True)
    async def native_noted_at_half_past_escaped(self) -> list[int]: ...

    @query(
        "SELECT id FROM qa_author -- :unused, in a comment, is not a parameter\nWHERE region = :region ORDER BY id",
        native=True,
    )
    async def commented(self, region: str) -> list[int]: ...


async def _authors(datasources: Datasources) -> AuthorRepository:
    """Authors 1 (Ana, eu, active, 2 books), 2 (Bob, eu, active, no book), 3 (Cid, us, inactive, 1 book) and 4
    (Dee, us, active, note 'a.n/a.')."""
    rows = [
        {"id": 1, "dname": "Ana", "active": True, "note": None, "region": "eu"},
        {"id": 2, "dname": "Bob", "active": True, "note": None, "region": "eu"},
        {"id": 3, "dname": "Cid", "active": False, "note": "status = true", "region": "us"},
        {"id": 4, "dname": "Dee", "active": True, "note": "a.n/a.", "region": "us"},
    ]
    async with datasources.engine.begin() as conn:
        for row in rows:
            row.update(token=uuid.uuid4(), joined_at=datetime(2026, 1, row["id"], tzinfo=UTC))
        await conn.execute(insert(QaAuthor.__table__), rows)
        await conn.execute(
            insert(QaBook), [{"id": 10, "author_id": 1, "title": "t1"}, {"id": 11, "author_id": 1, "title": "t2"}]
        )
        await conn.execute(insert(QaBook), [{"id": 30, "author_id": 3, "title": "t3"}])
    return RepositoryBeanPostProcessor().after_init(AuthorRepository(), "authors")


def _ids(items: list[Any]) -> list[int]:
    return [item.id for item in items]


# ---------------------------------------------------------------------------------------------------------
# WP04-03 (C048, C050): entity results are the unit of work's entities
# ---------------------------------------------------------------------------------------------------------


async def test_entity_results_are_identity_mapped_and_their_changes_commit(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)

        @transactional
        async def rename_through_a_query() -> bool:
            ana = await authors.find_named("Ana")
            assert ana is not None
            same = ana is await authors.find_by_id(1)
            ana.note = "changed in the unit"
            return same

        assert await rename_through_a_query() is True
        ana = await authors.find_by_id(1)
        assert ana is not None and ana.note == "changed in the unit"


async def test_saving_a_query_result_updates_its_row(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        bob = await authors.find_named("Bob")
        assert bob is not None
        bob.note = "saved"
        await authors.save(bob)  # an UPDATE of the detached entity, not a second INSERT
        assert (await authors.find_by_id(2)).note == "saved"  # type: ignore[union-attr]
        await authors.delete(bob)
        assert await authors.find_by_id(2) is None


async def test_entity_results_are_typed_mapped_by_attribute_and_loaded(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        ana = await authors.find_named("Ana")
        assert ana is not None
        assert ana.display_name == "Ana"  # the attribute of the "dname" column
        assert isinstance(ana.token, uuid.UUID) and ana.active is True
        assert ana.joined_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert [book.title for book in ana.books] == ["t1", "t2"]  # the selectin relationship is loaded
        again = await authors.find_by_token_query(ana.token)
        assert again is not None and again.id == 1
        native = await authors.native_in_region("us")
        assert [(author.id, author.display_name, author.active) for author in native] == [
            (3, "Cid", False),
            (4, "Dee", True),
        ]


async def test_relationships_loaded_with_a_join_are_loaded_for_query_results(
    relational_backend: RelationalBackend,
) -> None:
    """A text statement cannot be joined to the relationships a mapping loads with a join: they are loaded with
    one more statement each instead, so they are there on the detached results."""
    async with repository_datasources(relational_backend, QaAuthor, QaBook, QaShelf, QaShelfBook) as datasources:
        await _authors(datasources)
        async with datasources.engine.begin() as conn:
            await conn.execute(insert(QaShelf), [{"id": 1}, {"id": 2}])
            await conn.execute(insert(QaShelfBook), [{"id": 5, "shelf_id": 1}, {"id": 6, "shelf_id": 1}])
        books = RepositoryBeanPostProcessor().after_init(BookRepository(), "books")
        found = await books.titled("t%")
        assert [(book.id, book.author.display_name) for book in found] == [(10, "Ana"), (11, "Ana"), (30, "Cid")]
        shelves = RepositoryBeanPostProcessor().after_init(ShelfRepository(), "shelves")
        assert [[book.id for book in shelf.books] for shelf in await shelves.every()] == [[5, 6], []]


async def test_relationships_loaded_with_a_subquery_are_loaded_for_query_results(
    relational_backend: RelationalBackend,
) -> None:
    """A text statement cannot be the subquery a ``lazy="subquery"`` relationship loads from either: those load with
    one more statement too, so they are there on the detached results."""
    async with repository_datasources(relational_backend, QaCrate, QaCrateItem) as datasources:
        async with datasources.engine.begin() as conn:
            await conn.execute(insert(QaCrate), [{"id": 1}, {"id": 2}])
            await conn.execute(insert(QaCrateItem), [{"id": 7, "crate_id": 1}, {"id": 8, "crate_id": 1}])
        crates = RepositoryBeanPostProcessor().after_init(CrateRepository(), "crates")
        assert [[item.id for item in crate.items] for crate in await crates.every()] == [[7, 8], []]


async def test_a_query_sees_the_units_pending_changes(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)

        @transactional
        async def move_then_query() -> tuple[list[int], list[str], dict[str, Any] | None]:
            bob = await authors.find_by_id(2)
            assert bob is not None
            bob.region = "ap"  # pending: not flushed yet
            return _ids(await authors.native_in_region("ap")), await authors.names_in("ap"), await authors.row_of(2)

        found, names, row = await move_then_query()
        assert (found, names) == ([2], ["Bob"])
        assert row == {"dname": "Bob", "region": "ap"}


# ---------------------------------------------------------------------------------------------------------
# WP04-04 (C049, C051): the JPQL transpiler
# ---------------------------------------------------------------------------------------------------------


async def test_boolean_literals_work_on_every_backend(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert _ids(await authors.find_active()) == [1, 2, 4]
        assert sorted(_ids(await authors.find_inactive_or_odd())) == [3]  # 'status = true' stays a literal


async def test_string_literals_and_correlated_subqueries_are_kept(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert _ids(await authors.find_noted("us")) == [4]
        assert _ids(await authors.find_with_books()) == [1, 3]
        assert await authors.count_with_books() == 2
        assert sorted(_ids(await authors.find_without_books())) == [2, 4]


async def test_a_colon_in_a_literal_or_a_comment_is_never_a_parameter(relational_backend: RelationalBackend) -> None:
    """``'10:30'`` and ``'10\\:30'`` (the escape ``text()`` documents, which queries written before carry) both
    match a row noted ``10:30``, in JPQL and in native SQL; a ``:name`` in a comment binds nothing."""
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        async with datasources.engine.begin() as conn:
            await conn.execute(update(QaAuthor).where(QaAuthor.id == 2).values(note="10:30"))
        for method in (
            authors.noted_at_half_past,
            authors.noted_at_half_past_escaped,
            authors.native_noted_at_half_past,
            authors.native_noted_at_half_past_escaped,
        ):
            assert await method() == [2], method.__name__
        assert await authors.commented("eu") == [1, 2]


async def _nodes(datasources: Datasources) -> NodeRepository:
    """The tree 1 <- 2 <- 3: node 3 is the only leaf."""
    async with datasources.engine.begin() as conn:
        await conn.execute(
            insert(QaNode),
            [{"id": 1, "parent_id": None}, {"id": 2, "parent_id": 1}, {"id": 3, "parent_id": 2}],
        )
    return RepositoryBeanPostProcessor().after_init(NodeRepository(), "nodes")


async def _node_rows(datasources: Datasources) -> list[tuple[int, int | None, bool]]:
    async with datasources.engine.connect() as conn:
        rows = (await conn.execute(select(QaNode.id, QaNode.parent_id, QaNode.leaf).order_by(QaNode.id))).all()
    return [(row[0], row[1], bool(row[2])) for row in rows]


@pytest.mark.backends("sqlite-file", "pg", "mariadb")
async def test_a_modifying_statement_keeps_its_self_correlated_subquery(relational_backend: RelationalBackend) -> None:
    """Only the target's alias is dropped: a subquery over the target's own table keeps its alias, so it stays
    correlated with the row being changed (dropping every alias changed each node with a child, not the leaves)."""
    async with repository_datasources(relational_backend, QaNode) as datasources:
        nodes = await _nodes(datasources)
        assert await nodes.mark_leaves() == 1
        assert await _node_rows(datasources) == [(1, None, False), (2, 1, False), (3, 2, True)]
        assert await nodes.prune_leaves() == 1
        assert await _node_rows(datasources) == [(1, None, False), (2, 1, False)]
        # Inside the subquery, n is the subquery's own node: the parents of the nodes above 1 (node 1) go.
        assert await nodes.delete_parents_of_nodes_above(1) == 1
        assert await _node_rows(datasources) == [(2, 1, False)]


@pytest.mark.backends("mysql")
async def test_mysql_refuses_a_modifying_statement_whose_subquery_reads_its_target(
    relational_backend: RelationalBackend,
) -> None:
    """MySQL does not let an ``UPDATE`` or a ``DELETE`` read its own table in a subquery (error 1093, which MariaDB
    lifted): the statement fails, and changes nothing."""
    async with repository_datasources(relational_backend, QaNode) as datasources:
        nodes = await _nodes(datasources)
        for statement in (nodes.mark_leaves, nodes.prune_leaves):
            with pytest.raises(DBAPIError) as caught:
                await statement()
            assert caught.value.orig is not None and caught.value.orig.args[0] == 1093
        assert await _node_rows(datasources) == [(1, None, False), (2, 1, False), (3, 2, False)]


# ---------------------------------------------------------------------------------------------------------
# WP04-06 (C127): shapes from the annotation, modifying statements, arguments
# ---------------------------------------------------------------------------------------------------------


async def test_results_follow_the_return_annotation(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert await authors.names_in("eu") == ["Ana", "Bob"]
        assert await authors.is_active(3) is False
        assert await authors.is_active(99) is None
        assert await authors.pairs() == [(1, "Ana"), (2, "Bob"), (3, "Cid"), (4, "Dee")]
        views = await authors.views("us")
        assert [(view.id, view.display_name) for view in views] == [(3, "Cid"), (4, "Dee")]
        assert await authors.row_of(3) == {"dname": "Cid", "region": "us"}
        assert await authors.row_of(99) is None
        assert await authors.has_title("t3") is True
        assert await authors.has_title("nope") is False
        with pytest.raises(IncorrectResultSizeException):
            await authors.find_one_in_region("eu")


async def test_arguments_bind_by_position_keyword_and_jpql_position(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert _ids(await authors.find_in_region("eu", True)) == [1, 2]
        assert _ids(await authors.find_in_region(active=False, region="us")) == [3]
        found = await authors.find_named("Cid")
        assert found is not None and found.id == 3
        assert _ids(await authors.find_ids([4, 1, 9])) == [1, 4]
        assert await authors.find_ids([]) == []


async def test_modifying_statements_return_their_row_count(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert await authors.deactivate("eu", note="gone") == 2
        assert [(a.id, a.active, a.note) for a in await authors.native_in_region("eu")] == [
            (1, False, "gone"),
            (2, False, "gone"),
        ]
        assert await authors.move(1, region="us") is None
        assert await authors.purge("eu") == 1  # Bob has no book; Ana moved away
        async with datasources.engine.connect() as conn:
            ids = (await conn.execute(text("SELECT id FROM qa_author ORDER BY id"))).scalars().all()
        assert list(ids) == [1, 3, 4]


async def test_a_modifying_statement_flushes_first_and_can_clear_the_unit(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)

        @transactional
        async def change_then_purge() -> tuple[int, bool]:
            bob = await authors.find_by_id(2)
            assert bob is not None
            bob.region = "xx"  # flushed before the DELETE runs, so the DELETE does not see Bob in "eu"
            purged = await authors.purge("eu")
            return purged, bob in authors._session

        purged, still_held = await change_then_purge()
        assert (purged, still_held) == (0, False)  # clear_automatically detached what the unit held
        assert (await authors.find_by_id(2)).region == "xx"  # type: ignore[union-attr]


async def test_a_modifying_statement_raises_the_kernels_exception(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        with pytest.raises(DataIntegrityException):
            await authors.rename(2, name="Ana")  # the unique display name
        assert (await authors.find_by_id(2)).display_name == "Bob"  # type: ignore[union-attr]


@pytest.mark.backends("sqlite-file", "pg", "mysql")  # MariaDB accepts a WITH clause before a SELECT only
async def test_a_modifying_statement_may_start_with_a_with_clause(relational_backend: RelationalBackend) -> None:
    """The statement's verb is read after its ``WITH`` clause: a ``WITH ... UPDATE`` is a ``@modifying``
    statement, and returns its row count."""
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        authors = await _authors(datasources)
        assert await authors.note_authors_of("t1", note="wrote t1") == 1
        assert [(a.id, a.note) for a in await authors.native_in_region("eu")] == [(1, "wrote t1"), (2, None)]


class BookPurgeRepository(Repository[QaBook, int]):
    @query("WITH gone AS (DELETE FROM qa_book WHERE title = :title RETURNING id) SELECT id FROM gone", native=True)
    async def take(self, title: str) -> list[int]: ...


@pytest.mark.backends("pg")
async def test_a_select_over_a_data_modifying_with_clause_runs_in_a_write_unit(
    relational_backend: RelationalBackend,
) -> None:
    """A ``SELECT`` whose ``WITH`` clause deletes (PostgreSQL) returns what it selects, and runs in a write unit: a
    read unit is ``READ ONLY`` there and refused the ``DELETE``."""
    async with repository_datasources(relational_backend, QaAuthor, QaBook) as datasources:
        await _authors(datasources)
        books = RepositoryBeanPostProcessor().after_init(BookPurgeRepository(), "books")
        assert await books.take("t3") == [30]
        async with datasources.engine.connect() as conn:
            left = (await conn.execute(text("SELECT id FROM qa_book ORDER BY id"))).scalars().all()
        assert list(left) == [10, 11]
