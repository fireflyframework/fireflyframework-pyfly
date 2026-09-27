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
"""Soft-deleted rows stay invisible in every ORM load path, on every session (C057).

The filter used to exist only inside ``SoftDeleteRepository``'s own root reads, so a deleted comment was
still in ``post.comments`` (selectin, lazy and joined loads, a refresh, an explicit ``selectinload``), a
live comment's ``.post`` returned its deleted parent, and a join matched through deleted rows. The
``SoftDeleteMixin`` loader criteria (``pyfly.data.relational.sqlalchemy.soft_delete_criteria``) apply to
every ORM ``SELECT`` of every session: the registry's, an application's own session factory (which the
primary transaction manager uses then), a manual session. ``include_deleted`` opts a statement out, and
``including_deleted()`` a block. The sqlite-file lane runs in the fast suite, the server lanes in the
integration suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, Session, joinedload, mapped_column, relationship, selectinload

from pyfly.container import bean, configuration, repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import INCLUDE_DELETED, including_deleted
from pyfly.data.relational.sqlalchemy.specification import Specification
from tests.support.backend_matrix import RelationalBackend


class SoftAuthor(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_author"

    name: Mapped[str] = mapped_column(String(50))
    books: Mapped[list[SoftBook]] = relationship(back_populates="author", lazy="selectin", order_by="SoftBook.title")
    lazy_books: Mapped[list[SoftBook]] = relationship(viewonly=True, lazy="select", order_by="SoftBook.title")


class SoftBook(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_book"

    title: Mapped[str] = mapped_column(String(50))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_author.id"))
    author: Mapped[SoftAuthor] = relationship(back_populates="books", lazy="select")


class SoftAuthorRepository(SoftDeleteRepository[SoftAuthor, uuid.UUID]):
    pass


class SoftBookRepository(Repository[SoftBook, uuid.UUID]):
    pass


class _Library:
    """Alice has a live and a deleted book; Dora is deleted and has a live book."""

    def __init__(self) -> None:
        self.alice = SoftAuthor(name="alice")
        self.dora = SoftAuthor(name="dora", deleted_at=datetime.now(UTC))
        self.live = SoftBook(title="a-live", author=self.alice)
        self.dead = SoftBook(title="a-dead", author=self.alice, deleted_at=datetime.now(UTC))
        self.orphan = SoftBook(title="d-live", author=self.dora)


async def _library(backend: RelationalBackend) -> tuple[async_sessionmaker[AsyncSession], _Library]:
    await backend.create_tables(SoftAuthor, SoftBook)
    factory = async_sessionmaker(backend.create_engine(), expire_on_commit=False)
    library = _Library()
    async with factory() as session, session.begin():
        session.add_all([library.alice, library.dora, library.live, library.dead, library.orphan])
    return factory, library


def _titles(books: list[SoftBook]) -> list[str]:
    return [book.title for book in books]


async def test_collections_hide_deleted_children_in_every_loader(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        alice = await SoftAuthorRepository(session=session).find_by_id(library.alice.id)
        assert alice is not None
        assert _titles(alice.books) == ["a-live"]  # selectin
        assert _titles(await session.run_sync(lambda _sync: alice.lazy_books)) == ["a-live"]  # lazy select

    for loader in (selectinload, joinedload):
        async with factory() as session:
            stmt = select(SoftAuthor).options(loader(SoftAuthor.lazy_books)).order_by(SoftAuthor.name)
            authors = (await session.execute(stmt)).unique().scalars().all()
            assert {author.name: _titles(author.lazy_books) for author in authors} == {"alice": ["a-live"]}

    async with factory() as session:
        alice = await session.get(SoftAuthor, library.alice.id)
        assert alice is not None
        await session.refresh(alice)
        assert _titles(alice.books) == ["a-live"]


async def test_a_live_child_does_not_reach_its_deleted_parent(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        orphan = await session.get(SoftBook, library.orphan.id)
        assert orphan is not None
        assert await session.run_sync(lambda _sync: orphan.author) is None


async def test_joins_do_not_match_through_deleted_rows(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        through_child = select(SoftAuthor).join(SoftAuthor.books).where(SoftBook.title == "a-dead")
        assert (await session.execute(through_child)).unique().scalars().all() == []
        through_parent = select(SoftBook).join(SoftBook.author).where(SoftAuthor.name == "dora")
        assert (await session.execute(through_parent)).scalars().all() == []

        spec: Specification[SoftAuthor] = Specification(
            lambda root, query: query.join(SoftAuthor.books).where(SoftBook.title == "a-dead")
        )
        assert await SoftAuthorRepository(session=session).find_all_by_spec(spec) == []


async def test_every_repository_and_count_skip_deleted_rows(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        books = SoftBookRepository(session=session)  # a plain Repository over a soft-delete entity
        assert sorted(_titles(await books.find_all())) == ["a-live", "d-live"]
        assert await books.count() == 2
        assert await books.find_by_id(library.dead.id) is None
        count = select(func.count()).select_from(SoftBook)
        assert (await session.execute(count)).scalar_one() == 2


async def test_include_deleted_and_including_deleted_opt_out(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        assert await session.get(SoftAuthor, library.dora.id) is None
        found = await session.get(SoftAuthor, library.dora.id, execution_options={INCLUDE_DELETED: True})
        assert found is not None and found.is_deleted
    async with factory() as session:
        every_book = select(SoftBook.title).order_by(SoftBook.title).execution_options(include_deleted=True)
        assert (await session.execute(every_book)).scalars().all() == ["a-dead", "a-live", "d-live"]
    async with factory() as session:
        with including_deleted():
            assert await SoftBookRepository(session=session).count() == 3
        assert await SoftBookRepository(session=session).count() == 2


async def test_soft_delete_repository_still_reaches_deleted_rows_where_it_must(
    relational_backend: RelationalBackend,
) -> None:
    factory, library = await _library(relational_backend)
    async with factory() as session:
        authors = SoftAuthorRepository(session=session)
        assert sorted(a.name for a in await authors.find_all_including_deleted()) == ["alice", "dora"]
        restored = await authors.restore(library.dora.id)
        assert restored is not None and not restored.is_deleted
        await session.commit()
    async with factory() as session:
        authors = SoftAuthorRepository(session=session)
        assert await authors.find_by_id(library.dora.id) is not None

        # hard_delete removes a soft-deleted row too.
        await SoftBookRepository(session=session).delete_by_id(library.orphan.id)  # a plain hard delete
        await authors.delete_by_id(library.dora.id)
        await session.commit()
    async with factory() as session:
        await SoftAuthorRepository(session=session).hard_delete(library.dora.id)
        await session.commit()
    async with factory() as session:
        with including_deleted():
            assert await session.get(SoftAuthor, library.dora.id) is None


async def test_an_entity_created_in_the_session_lazy_loads_without_deleted_children(
    relational_backend: RelationalBackend,
) -> None:
    """A lazy load has no parent query to inherit the criteria from when the parent was created in this
    session and never loaded; the lazy load gets them itself."""
    await relational_backend.create_tables(SoftAuthor, SoftBook)
    factory = async_sessionmaker(relational_backend.create_engine(), expire_on_commit=False)
    async with factory() as session:
        author = SoftAuthor(name="fresh")
        session.add(author)
        await session.flush()
        session.add_all(
            [
                SoftBook(title="fresh-live", author_id=author.id),
                SoftBook(title="fresh-dead", author_id=author.id, deleted_at=datetime.now(UTC)),
            ]
        )
        await session.flush()
        assert _titles(await session.run_sync(lambda _sync: author.lazy_books)) == ["fresh-live"]
        await session.rollback()


class _CustomSession(Session):
    """An application's own sync Session class."""


async def test_a_custom_session_class_is_filtered_too(relational_backend: RelationalBackend) -> None:
    factory, library = await _library(relational_backend)
    custom = async_sessionmaker(factory.kw["bind"], expire_on_commit=False, sync_session_class=_CustomSession)
    async with custom() as session:
        alice = await session.get(SoftAuthor, library.alice.id)
        assert alice is not None and _titles(alice.books) == ["a-live"]
        assert await session.get(SoftAuthor, library.dora.id) is None


# ---------------------------------------------------------------------------------------------------------
# Managed repositories and @transactional, on the registry's sessions and on an application's own factory
# ---------------------------------------------------------------------------------------------------------


@repository
class ManagedAuthorRepository(SoftDeleteRepository[SoftAuthor, uuid.UUID]):
    pass


@service
class LibraryService:
    def __init__(self, authors: ManagedAuthorRepository) -> None:
        self._authors = authors

    @transactional
    async def book_titles(self, author_id: uuid.UUID) -> list[str]:
        author = await self._authors.find_by_id(author_id)
        assert author is not None
        return _titles(author.books)


_APPLICATION_FACTORY: dict[str, Any] = {}


@configuration
class _ApplicationSessions:
    @bean
    def application_sessions(self) -> async_sessionmaker[AsyncSession]:
        factory: async_sessionmaker[AsyncSession] = _APPLICATION_FACTORY["factory"]
        return factory


async def _managed_titles(backend: RelationalBackend, *, application_factory: bool) -> tuple[list[str], list[str]]:
    factory, library = await _library(backend)
    ctx = ApplicationContext(backend.config())
    if application_factory:
        _APPLICATION_FACTORY["factory"] = factory
        ctx.register_bean(_ApplicationSessions)
    ctx.register_bean(ManagedAuthorRepository)
    ctx.register_bean(LibraryService)
    await ctx.start()
    try:
        if application_factory:
            assert ctx.get_bean(async_sessionmaker) is factory
        in_transaction = await ctx.get_bean(LibraryService).book_titles(library.alice.id)
        alice = await ctx.get_bean(ManagedAuthorRepository).find_by_id(library.alice.id)  # an auto unit
        assert alice is not None
        return in_transaction, _titles(alice.books)
    finally:
        await ctx.stop()


async def test_managed_repositories_on_the_registry_sessions(relational_backend: RelationalBackend) -> None:
    assert await _managed_titles(relational_backend, application_factory=False) == (["a-live"], ["a-live"])


async def test_managed_repositories_on_an_application_session_factory(relational_backend: RelationalBackend) -> None:
    """The primary transaction manager serves the application's own factory: the criteria do not depend on
    the registry having built it."""
    assert await _managed_titles(relational_backend, application_factory=True) == (["a-live"], ["a-live"])
