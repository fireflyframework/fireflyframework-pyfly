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
"""Specification ``|`` and ``~`` on every lane (C058).

``|`` and ``~`` combine what their operands *match*, as criteria: a specification that joins another table is an
``EXISTS`` over its own statement, so its join never leaks into the combined query (no cartesian product, no
rows dropped by an inner join the other operand did not ask for), and the base query's own criteria (a
``SoftDeleteRepository``'s ``deleted_at IS NULL``) are never copied into, or negated with, an operand. A
specification that adds nothing is absent, as in Spring: ``noop | admin`` is ``admin`` and ``~noop`` restricts
nothing, on a plain and on a soft-delete repository alike.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String, event, insert
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.data.pageable import Pageable, Sort
from pyfly.data.relational.sqlalchemy.entity import Base, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.filter import FilterOperator, FilterUtils
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.specification import Specification
from tests.integration._repository_harness import Datasources, repository_datasources
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")  # a cartesian product warns


class SpAuthor(Base):
    __tablename__ = "sp_author"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(20))
    books: Mapped[list[SpBook]] = relationship(back_populates="author", order_by="SpBook.id")


class SpBook(SoftDeleteMixin, Base):
    __tablename__ = "sp_book"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("sp_author.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(20))
    author: Mapped[SpAuthor | None] = relationship(back_populates="books")


class SpMember(SoftDeleteMixin, Base):
    __tablename__ = "sp_member"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    role: Mapped[str] = mapped_column(String(20))
    team: Mapped[str] = mapped_column(String(20))


class AuthorRepository(Repository[SpAuthor, int]):
    pass


class BookRepository(Repository[SpBook, int]):
    pass


class MemberRepository(Repository[SpMember, int]):
    pass


class SoftMemberRepository(SoftDeleteRepository[SpMember, int]):
    pass


MODELS = (SpAuthor, SpBook, SpMember)

HAS_T1 = Specification[SpAuthor](
    lambda root, q: q.join(SpBook, SpBook.author_id == root.id).where(SpBook.title == "t1")
)
"""Authors with a book titled t1, through an explicit join (a collection: it repeats an author per book)."""

HAS_T2 = Specification[SpAuthor](lambda root, q: q.join(root.books).where(SpBook.title == "t2"))
"""The same through the relationship."""

NAMED_Z = Specification[SpAuthor](lambda root, q: q.where(root.name == "Z"))

NOOP = Specification[Any](lambda root, q: q)


async def _library(datasources: Datasources) -> None:
    """Authors A (t1, t2, and a soft-deleted t9), B (t2), C (no book) and Z (no book); an orphan book."""
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(SpAuthor), [{"id": n, "name": name} for n, name in enumerate("ABCZ", start=1)])
        await conn.execute(
            insert(SpBook),
            [
                {"id": 10, "author_id": 1, "title": "t1", "deleted_at": None},
                {"id": 11, "author_id": 1, "title": "t2", "deleted_at": None},
                {"id": 12, "author_id": 1, "title": "t9", "deleted_at": datetime(2020, 1, 1, tzinfo=UTC)},
                {"id": 20, "author_id": 2, "title": "t2", "deleted_at": None},
                {"id": 30, "author_id": None, "title": "orphan", "deleted_at": None},
            ],
        )


def _names(items: list[Any]) -> list[str]:
    return sorted(item.name for item in items)


async def test_or_and_not_of_a_joining_specification_match_what_it_matches(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _library(datasources)
        authors = AuthorRepository()
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            assert _names(await authors.find_all_by_spec(HAS_T1)) == ["A"]
            assert _names(await authors.find_all_by_spec(HAS_T1 | NAMED_Z)) == ["A", "Z"]  # Z has no book
            assert _names(await authors.find_all_by_spec(NAMED_Z | HAS_T1)) == ["A", "Z"]
            assert _names(await authors.find_all_by_spec(~HAS_T1)) == ["B", "C", "Z"]  # each once
            assert _names(await authors.find_all_by_spec(HAS_T1 & NAMED_Z)) == []
            assert _names(await authors.find_all_by_spec(HAS_T2 | HAS_T1)) == ["A", "B"]
            assert _names(await authors.find_all_by_spec(~(HAS_T1 | HAS_T2))) == ["C", "Z"]
            assert _names(await authors.find_all_by_spec(~HAS_T2 & ~NAMED_Z)) == ["C"]
            page = await authors.find_all_by_spec_paged(~HAS_T1, Pageable.of(1, 2, Sort.by("name")))
            assert (page.total, [author.name for author in page.items]) == (3, ["B", "C"])


async def test_a_soft_deleted_row_does_not_match_inside_an_operand(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _library(datasources)
        has_t9 = Specification[SpAuthor](lambda root, q: q.join(root.books).where(SpBook.title == "t9"))
        authors = AuthorRepository()
        assert _names(await authors.find_all_by_spec(has_t9 | NAMED_Z)) == ["Z"]
        assert _names(await authors.find_all_by_spec(~has_t9)) == ["A", "B", "C", "Z"]


async def test_or_of_a_many_to_one_join_keeps_the_rows_without_a_parent(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _library(datasources)
        by_a = Specification[SpBook](lambda root, q: q.join(root.author).where(SpAuthor.name == "A"))
        orphan = Specification[SpBook](lambda root, q: q.where(root.title == "orphan"))
        books = BookRepository()
        assert sorted(book.id for book in await books.find_all_by_spec(by_a | orphan)) == [10, 11, 30]
        assert sorted(book.id for book in await books.find_all_by_spec(~by_a)) == [20, 30]


async def test_an_empty_specification_is_absent_on_every_repository(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        async with datasources.engine.begin() as conn:
            rows = [{"id": 1, "role": "admin", "team": "a"}, {"id": 2, "role": "user", "team": "a"}]
            rows.append({"id": 3, "role": "user", "team": "b"})
            await conn.execute(insert(SpMember), rows)
        admin = FilterOperator.eq("role", "admin")
        for members in (MemberRepository(), SoftMemberRepository()):
            assert [m.id for m in await members.find_all_by_spec(NOOP | admin)] == [1]
            assert [m.id for m in await members.find_all_by_spec(admin | NOOP)] == [1]
            assert sorted(m.id for m in await members.find_all_by_spec(~NOOP)) == [1, 2, 3]
            assert sorted(m.id for m in await members.find_all_by_spec(~FilterUtils.from_dict({}))) == [1, 2, 3]
            assert sorted(m.id for m in await members.find_all_by_spec(~admin)) == [2, 3]


async def test_combined_specifications_grow_the_sql_linearly(relational_backend: RelationalBackend) -> None:
    """Each | used to copy the incoming WHERE into both branches: 3^k copies of the base criteria for k groups."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        members = SoftMemberRepository()
        spec = NOOP
        for group in range(8):
            spec = spec & (FilterOperator.eq("team", f"t{group}") | FilterOperator.eq("role", f"r{group}"))
        sql: list[str] = []

        def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            sql.append(statement)

        event.listen(datasources.engine.sync_engine, "before_cursor_execute", record)
        try:
            assert await members.find_all_by_spec(spec) == []
        finally:
            event.remove(datasources.engine.sync_engine, "before_cursor_execute", record)
        (statement,) = [text_ for text_ in sql if "sp_member" in text_]
        assert statement.count("deleted_at IS NULL") <= 2  # the repository's and the global criteria, once each
        assert len(statement) < 2000
