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
  rewritten only where it is one (never inside a string literal or another identifier), and a correlated
  subquery keeps its correlation.
- The result is shaped by the return annotation, not guessed from the SQL (C127): a list query with an
  ``EXISTS`` subquery returns a list, scalars, rows and projections come back as declared, ``@modifying``
  statements return their row count, and arguments bind by position or keyword (``?1`` in JPQL too).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

import pytest
from sqlalchemy import Boolean, ForeignKey, Integer, String, Uuid, insert, text
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
