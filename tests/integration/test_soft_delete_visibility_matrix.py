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

import contextlib
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import ForeignKey, String, func, select
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, Session, joinedload, lazyload, mapped_column, relationship, selectinload

from pyfly.container import bean, configuration, repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.soft_delete_criteria import INCLUDE_DELETED, hard_delete, including_deleted
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.data.transaction import Propagation
from pyfly.testing import StatementCounter
from tests.integration._repository_harness import repository_datasources
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


class AuthorArchive(Repository[SoftAuthor, uuid.UUID]):
    """A plain Repository over a soft-delete entity, with a retention query."""

    async def find_by_deleted_at_less_than(self, cutoff: datetime) -> list[SoftAuthor]: ...


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


async def test_including_deleted_lifts_the_criteria_but_not_soft_delete_repository_reads(
    relational_backend: RelationalBackend,
) -> None:
    """The documented opt-out: inside ``including_deleted()`` a plain Repository (its derived queries too) and
    ORM statements see deleted rows. ``SoftDeleteRepository``'s own reads add ``deleted_at IS NULL``
    themselves and keep excluding them there; ``find_all_including_deleted()`` is their opt-out."""
    factory, _ = await _library(relational_backend)
    cutoff = datetime.now(UTC) + timedelta(minutes=1)
    async with factory() as session:
        archive = AuthorArchive(session=session)
        RepositoryBeanPostProcessor().after_init(archive, "authorArchive")
        authors = SoftAuthorRepository(session=session)
        assert await archive.find_by_deleted_at_less_than(cutoff) == []
        with including_deleted():
            assert [a.name for a in await archive.find_by_deleted_at_less_than(cutoff)] == ["dora"]
            assert sorted(a.name for a in await archive.find_all()) == ["alice", "dora"]
            every_author = select(SoftAuthor.name).order_by(SoftAuthor.name)
            assert (await session.scalars(every_author)).all() == ["alice", "dora"]

            assert [a.name for a in await authors.find_all()] == ["alice"]
            assert await authors.count() == 1
            assert sorted(a.name for a in await authors.find_all_including_deleted()) == ["alice", "dora"]


async def test_merging_a_detached_soft_deleted_object_needs_including_deleted(
    relational_backend: RelationalBackend,
) -> None:
    """``session.merge()`` loads the row it merges into through the criteria: a detached soft-deleted object
    finds none, so the merge INSERTs a copy and the flush violates the primary key. Inside
    ``including_deleted()`` it finds the row and updates it."""
    factory, library = await _library(relational_backend)
    detached = SoftAuthor(id=library.dora.id, name="dora-renamed", deleted_at=library.dora.deleted_at)
    async with factory() as session:
        merged = await session.merge(detached)
        with pytest.raises(IntegrityError):
            await session.flush()
        assert merged is not detached
    async with factory() as session:
        with including_deleted():
            await session.merge(detached)
            await session.commit()
    async with factory() as session:
        found = await session.get(SoftAuthor, library.dora.id, execution_options={INCLUDE_DELETED: True})
        assert found is not None and found.name == "dora-renamed" and found.is_deleted


@pytest.mark.parametrize("repository", [AuthorArchive, SoftAuthorRepository])
async def test_saving_a_soft_deleted_entity_returns_it_loaded(
    relational_backend: RelationalBackend, repository: type[Repository[SoftAuthor, uuid.UUID]]
) -> None:
    """``save`` reads back what the flush left unloaded (a ``selectin`` collection here) from the entity's own
    row, which the criteria hide once the entity is soft-deleted: it is read all the same, so the returned
    entity is as loaded as a live one's."""
    await relational_backend.create_tables(SoftAuthor, SoftBook)
    factory = async_sessionmaker(relational_backend.create_engine(), expire_on_commit=False)
    async with factory() as session:
        authors = repository(session=session)
        archived = await authors.save(SoftAuthor(name="archived", deleted_at=datetime.now(UTC)))
        await session.commit()
    assert archived.books == []  # loaded by save: the session is closed, so a lazy load would raise


@pytest.mark.parametrize("repository", [AuthorArchive, SoftAuthorRepository])
@pytest.mark.parametrize("expired", ["every_attribute", "deleted_at"])
async def test_saving_an_expired_entity_reads_it_back_in_the_unit(
    relational_backend: RelationalBackend, repository: type[Repository[SoftAuthor, uuid.UUID]], expired: str
) -> None:
    """``save`` tells a soft-deleted entity from a live one by the state it holds: reading ``deleted_at`` on an
    entity that expired it would load the row outside the unit's greenlet (``MissingGreenlet``, a 500). An
    entity whose ``deleted_at`` is not loaded is read back as a live one."""
    factory, library = await _library(relational_backend)
    async with factory() as session:
        authors = repository(session=session)
        alice = await authors.find_by_id(library.alice.id)
        assert alice is not None
        session.expire(alice, None if expired == "every_attribute" else ["deleted_at"])
        saved = await authors.save(alice)
        assert saved is alice and saved.name == "alice" and saved.deleted_at is None
        assert _titles(saved.books) == ["a-live"]


@pytest.mark.parametrize("repository", [AuthorArchive, SoftAuthorRepository])
async def test_saving_a_detached_entity_of_a_session_that_expires_on_commit(
    relational_backend: RelationalBackend, repository: type[Repository[SoftAuthor, uuid.UUID]]
) -> None:
    """An application's session factory may expire on commit: the entity it returns is detached with nothing
    loaded, and saving it unchanged in the next session reads it back there."""
    factory, library = await _library(relational_backend)
    expiring = async_sessionmaker(factory.kw["bind"], expire_on_commit=True)
    async with expiring() as session:
        alice = await repository(session=session).find_by_id(library.alice.id)
        assert alice is not None
        await session.commit()
    async with expiring() as session:
        saved = await repository(session=session).save(alice)
        assert saved.name == "alice" and _titles(saved.books) == ["a-live"]
        await session.commit()


class _StepFailed(Exception):
    pass


class _RenamingService:
    def __init__(self, authors: SoftAuthorRepository) -> None:
        self._authors = authors

    @transactional(propagation=Propagation.NESTED)
    async def risky_rename(self, author: SoftAuthor) -> None:
        author.name = "risky"
        await self._authors.save(author)
        raise _StepFailed

    @transactional
    async def create_with_a_fallback(self) -> tuple[str, list[str]]:
        author = await self._authors.save(SoftAuthor(name="first"))
        with contextlib.suppress(_StepFailed):  # the savepoint rolled back, expiring what it changed
            await self.risky_rename(author)
        saved = await self._authors.save(author)
        return saved.name, _titles(saved.books)


async def test_saving_after_a_nested_step_rolled_back(relational_backend: RelationalBackend) -> None:
    """A ``NESTED`` step that fails rolls its savepoint back, which expires the entities it changed; saving
    such an entity again in the outer unit reads it back there."""
    async with repository_datasources(relational_backend, SoftAuthor, SoftBook):
        assert await _RenamingService(SoftAuthorRepository()).create_with_a_fallback() == ("first", [])


async def test_saving_a_live_and_a_soft_deleted_entity_together(relational_backend: RelationalBackend) -> None:
    """``save_all`` reads a soft-deleted entity back with its deleted rows, and only that one: the live entity
    beside it gets its eager collection without the soft-deleted children."""
    factory, library = await _library(relational_backend)
    async with factory() as session:
        stmt = select(SoftAuthor).where(SoftAuthor.id == library.alice.id).options(lazyload(SoftAuthor.books))
        detached = (await session.execute(stmt)).scalar_one()
    assert "books" not in detached.__dict__
    async with factory() as session:
        alice, archived = await AuthorArchive(session=session).save_all(
            [detached, SoftAuthor(name="archived", deleted_at=datetime.now(UTC))]
        )
        await session.commit()
    assert _titles(alice.books) == ["a-live"]
    assert archived.books == []


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


async def test_a_restored_entity_comes_back_without_its_soft_deleted_children(
    relational_backend: RelationalBackend,
) -> None:
    """``restore`` reads the soft-deleted entity with ``include_deleted``, which reaches its eager loads, so its
    collections held the deleted rows; once restored it is a live entity, and they are loaded again without
    them. So is a collection the session loaded inside ``including_deleted()``, and so is an entity that was
    live already (nothing to restore)."""
    factory, library = await _library(relational_backend)
    async with factory() as session, session.begin():
        session.add(SoftBook(title="d-dead", author_id=library.dora.id, deleted_at=datetime.now(UTC)))
    async with factory() as session:
        authors = SoftAuthorRepository(session=session)
        restored = await authors.restore(library.dora.id)
        alice = await authors.restore(library.alice.id)
        await session.commit()
    assert restored is not None and not restored.is_deleted
    assert _titles(restored.books) == ["d-live"]
    assert alice is not None and not alice.is_deleted and _titles(alice.books) == ["a-live"]

    async with factory() as session:
        authors = SoftAuthorRepository(session=session)
        await authors.delete_by_id(library.dora.id)
        with including_deleted():
            dora = await session.get(SoftAuthor, library.dora.id)
            assert dora is not None
            assert _titles(await session.run_sync(lambda _sync: dora.lazy_books)) == ["d-dead", "d-live"]
        restored = await authors.restore(library.dora.id)
        assert restored is dora and not restored.is_deleted
        assert _titles(restored.books) == ["d-live"] and _titles(restored.lazy_books) == ["d-live"]
        await session.commit()


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


# ---------------------------------------------------------------------------------------------------------
# A hard delete reaches the soft-deleted rows its cascades and foreign keys depend on
# ---------------------------------------------------------------------------------------------------------
#
# The criteria hide a soft-deleted child from the collection load a delete cascade issues, so deleting the
# aggregate root left that child behind and the root's DELETE violated its foreign key. The repositories'
# hard deletes (and ``hard_delete()`` for a session used directly) load what the cascades reach, deleted
# rows included, even when the root and its collections were loaded, filtered, before the delete.


class Thread(BaseEntity):
    """A plain aggregate root: its posts cascade, its notes are only unlinked."""

    __tablename__ = "sd_thread"

    title: Mapped[str] = mapped_column(String(50))
    posts: Mapped[list[ThreadPost]] = relationship(cascade="all, delete-orphan", order_by="ThreadPost.body")
    notes: Mapped[list[ThreadNote]] = relationship(order_by="ThreadNote.body")


class ThreadPost(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_thread_post"

    body: Mapped[str] = mapped_column(String(50))
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_thread.id"))
    reactions: Mapped[list[ThreadReaction]] = relationship(cascade="all, delete-orphan")


class ThreadReaction(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_thread_reaction"

    emoji: Mapped[str] = mapped_column(String(20))
    post_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_thread_post.id"))


class ThreadNote(SoftDeleteMixin, BaseEntity):
    """No delete cascade: deleting the thread sets ``thread_id`` to NULL."""

    __tablename__ = "sd_thread_note"

    body: Mapped[str] = mapped_column(String(50))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sd_thread.id"), nullable=True)


class SoftThread(SoftDeleteMixin, BaseEntity):
    """A soft-delete aggregate root with soft-delete children."""

    __tablename__ = "sd_soft_thread"

    title: Mapped[str] = mapped_column(String(50))
    posts: Mapped[list[SoftThreadPost]] = relationship(cascade="all, delete-orphan")


class SoftThreadPost(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_soft_thread_post"

    body: Mapped[str] = mapped_column(String(50))
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_soft_thread.id"))


class Shelf(BaseEntity):
    """A plain aggregate root whose plain boxes hold soft-delete items."""

    __tablename__ = "sd_shelf"

    name: Mapped[str] = mapped_column(String(50))
    boxes: Mapped[list[ShelfBox]] = relationship(cascade="all, delete-orphan")


class ShelfBox(BaseEntity):
    __tablename__ = "sd_shelf_box"

    shelf_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_shelf.id"))
    items: Mapped[list[ShelfItem]] = relationship(cascade="all, delete-orphan")


class ShelfItem(SoftDeleteMixin, BaseEntity):
    __tablename__ = "sd_shelf_item"

    box_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sd_shelf_box.id"))


class ThreadRepository(Repository[Thread, uuid.UUID]):
    pass


class ShelfRepository(Repository[Shelf, uuid.UUID]):
    pass


class SoftThreadRepository(SoftDeleteRepository[SoftThread, uuid.UUID]):
    pass


class PlainSoftThreadRepository(Repository[SoftThread, uuid.UUID]):
    pass


_THREAD_TABLES = (Thread, ThreadPost, ThreadReaction, ThreadNote, SoftThread, SoftThreadPost)


async def _thread(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A thread with a live and a deleted post (each with a live and a deleted reaction), a live and a
    deleted note."""
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        thread = Thread(title="t")
        session.add(thread)
        await session.flush()
        live = ThreadPost(body="live", thread_id=thread.id)
        dead = ThreadPost(body="dead", thread_id=thread.id, deleted_at=now)
        session.add_all([live, dead])
        await session.flush()
        for post in (live, dead):
            session.add_all(
                [
                    ThreadReaction(emoji="up", post_id=post.id),
                    ThreadReaction(emoji="down", post_id=post.id, deleted_at=now),
                ]
            )
        session.add_all(
            [
                ThreadNote(body="live", thread_id=thread.id),
                ThreadNote(body="dead", thread_id=thread.id, deleted_at=now),
            ]
        )
        return thread.id


async def _soft_thread(factory: async_sessionmaker[AsyncSession], *, deleted: bool = False) -> uuid.UUID:
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        thread = SoftThread(title="s", deleted_at=now if deleted else None)
        session.add(thread)
        await session.flush()
        session.add_all(
            [
                SoftThreadPost(body="live", thread_id=thread.id),
                SoftThreadPost(body="dead", thread_id=thread.id, deleted_at=now),
            ]
        )
        return thread.id


async def _every_row(factory: async_sessionmaker[AsyncSession], model: type[BaseEntity]) -> int:
    async with factory() as session:
        stmt = select(func.count()).select_from(model).execution_options(include_deleted=True)
        return int((await session.execute(stmt)).scalar_one())


async def _unlinked_notes(factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, bool]]:
    async with factory() as session:
        stmt = select(ThreadNote).order_by(ThreadNote.body).execution_options(include_deleted=True)
        return [(note.body, note.thread_id is None) for note in (await session.execute(stmt)).scalars()]


async def _thread_factory(backend: RelationalBackend) -> async_sessionmaker[AsyncSession]:
    await backend.create_tables(*_THREAD_TABLES)
    return async_sessionmaker(backend.create_engine(), expire_on_commit=False)


@pytest.mark.parametrize("how", ["delete_by_id", "delete", "delete_all", "hard_delete"])
async def test_deleting_a_loaded_root_deletes_its_soft_deleted_children(
    relational_backend: RelationalBackend, how: str
) -> None:
    factory = await _thread_factory(relational_backend)
    thread_id = await _thread(factory)
    async with factory() as session:
        threads = ThreadRepository(session=session)
        thread = await threads.find_by_id(thread_id)  # loaded before the delete, as a screen shows it first
        assert thread is not None
        if how == "delete_by_id":
            await threads.delete_by_id(thread_id)
        elif how == "delete":
            await threads.delete(thread)
        elif how == "delete_all":
            await threads.delete_all([thread])
        else:
            await hard_delete(session, thread)
        await session.commit()

    assert [await _every_row(factory, model) for model in (Thread, ThreadPost, ThreadReaction)] == [0, 0, 0]
    assert await _unlinked_notes(factory) == [("dead", True), ("live", True)]


@pytest.mark.parametrize("how", ["delete_all_by_id", "delete_all", "delete_every_row"])
async def test_deleting_several_roots_through_the_orm_deletes_their_soft_deleted_children(
    relational_backend: RelationalBackend, how: str
) -> None:
    """The repository deletes of several roots at once reach the soft-deleted children too, whether the roots
    were loaded before or by the delete itself."""
    factory = await _thread_factory(relational_backend)
    thread_ids = [await _thread(factory), await _thread(factory)]
    async with factory() as session:
        threads = ThreadRepository(session=session)
        if how == "delete_all_by_id":
            await threads.delete_all_by_id(thread_ids)
        elif how == "delete_all":
            loaded = [await threads.find_by_id(thread_id) for thread_id in thread_ids]
            await threads.delete_all([thread for thread in loaded if thread is not None])
        else:
            await threads.delete_all()
        await session.commit()

    assert [await _every_row(factory, model) for model in (Thread, ThreadPost, ThreadReaction)] == [0, 0, 0]
    assert await _unlinked_notes(factory) == [("dead", True), ("dead", True), ("live", True), ("live", True)]


@pytest.mark.parametrize("how", ["delete_all_by_id", "delete_all", "delete_every_row"])
async def test_deleting_several_roots_loads_what_their_cascades_reach_once_for_all(
    relational_backend: RelationalBackend, how: str
) -> None:
    """The hard delete loads each level of what the delete reaches with the deleted rows once for all the
    objects of that level, not one object at a time: a ``SELECT`` of their keys and one per relationship
    (the threads' posts and notes, then the posts' reactions), however many roots are deleted. The
    repository's own load of the roots leaves those relationships to it."""
    factory = await _thread_factory(relational_backend)
    engine = factory.kw["bind"]
    selects: dict[int, int] = {}
    for roots in (2, 6):
        thread_ids = [await _thread(factory) for _ in range(roots)]
        async with factory() as session:
            threads = ThreadRepository(session=session)
            loaded = [await threads.find_by_id(thread_id) for thread_id in thread_ids] if how == "delete_all" else []
            with StatementCounter(engine) as counter:
                if how == "delete_all_by_id":
                    await threads.delete_all_by_id(thread_ids)
                elif how == "delete_all":
                    await threads.delete_all([thread for thread in loaded if thread is not None])
                else:
                    await threads.delete_all()
            await session.commit()
        selects[roots] = counter.counts().get("SELECT", 0)
        assert [await _every_row(factory, model) for model in (Thread, ThreadPost, ThreadReaction)] == [0, 0, 0]
    # The threads (unless the unit holds them), the keys and the two collections of the threads, the keys and
    # the reactions of the posts.
    expected = 5 if how == "delete_all" else 6
    assert selects == {2: expected, 6: expected}


async def test_deleting_a_root_after_one_of_its_children_in_the_same_unit(
    relational_backend: RelationalBackend,
) -> None:
    """A child deleted for good earlier in the unit stays in its parent's loaded collection (the ORM does not
    take it out): the hard delete of the parent passes over it, where expiring its collections failed on an
    object that is no longer persistent."""
    await relational_backend.create_tables(Shelf, ShelfBox, ShelfItem)
    factory = async_sessionmaker(relational_backend.create_engine(), expire_on_commit=False)
    async with factory() as session, session.begin():
        shelf = Shelf(name="s")
        session.add(shelf)
        await session.flush()
        for _ in range(2):
            box = ShelfBox(shelf_id=shelf.id)
            session.add(box)
            await session.flush()
            session.add_all([ShelfItem(box_id=box.id), ShelfItem(box_id=box.id, deleted_at=datetime.now(UTC))])
        shelf_id = shelf.id
    async with factory() as session:
        stmt = select(Shelf).where(Shelf.id == shelf_id).options(selectinload(Shelf.boxes).selectinload(ShelfBox.items))
        shelf = (await session.execute(stmt)).scalar_one()
        await hard_delete(session, shelf.boxes[0])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SAWarning)  # the ORM's cascade deletes that box again, and warns
            await ShelfRepository(session=session).delete(shelf)
        await session.commit()
    assert [await _every_row(factory, model) for model in (Shelf, ShelfBox, ShelfItem)] == [0, 0, 0]


async def test_deleting_a_root_whose_collections_were_loaded_filtered(relational_backend: RelationalBackend) -> None:
    """The collections were loaded without the deleted rows (a selectin load, a lazy load of a post loaded
    on its own): the hard delete reloads them with the deleted rows first."""
    factory = await _thread_factory(relational_backend)
    thread_id = await _thread(factory)
    async with factory() as session:
        stmt = (
            select(Thread)
            .where(Thread.id == thread_id)
            .options(selectinload(Thread.posts).selectinload(ThreadPost.reactions), selectinload(Thread.notes))
        )
        thread = (await session.execute(stmt)).scalar_one()
        assert [(post.body, [r.emoji for r in post.reactions]) for post in thread.posts] == [("live", ["up"])]
        assert [note.body for note in thread.notes] == ["live"]
        await ThreadRepository(session=session).delete(thread)
        await session.commit()

    assert [await _every_row(factory, model) for model in (Thread, ThreadPost, ThreadReaction)] == [0, 0, 0]
    assert await _unlinked_notes(factory) == [("dead", True), ("live", True)]


async def test_deleting_a_detached_root_deletes_its_soft_deleted_children(
    relational_backend: RelationalBackend,
) -> None:
    """A root loaded by another session: ``delete`` attaches it and loads its cascades with the deleted rows."""
    factory = await _thread_factory(relational_backend)
    thread_id = await _thread(factory)
    async with factory() as session:
        detached = await session.get(Thread, thread_id)
    assert detached is not None
    async with factory() as session:
        await ThreadRepository(session=session).delete(detached)
        await session.commit()

    assert [await _every_row(factory, model) for model in (Thread, ThreadPost, ThreadReaction)] == [0, 0, 0]
    assert await _unlinked_notes(factory) == [("dead", True), ("live", True)]


async def test_a_soft_delete_root_is_hard_deleted_with_its_soft_deleted_children(
    relational_backend: RelationalBackend,
) -> None:
    factory = await _thread_factory(relational_backend)
    live_id, deleted_id, plain_id = (
        await _soft_thread(factory),
        await _soft_thread(factory, deleted=True),
        await _soft_thread(factory),
    )
    async with factory() as session:
        threads = SoftThreadRepository(session=session)
        assert await threads.find_by_id(live_id) is not None  # loaded before the delete
        await threads.hard_delete(live_id)
        await threads.hard_delete(deleted_id)  # a soft-deleted root, too
        plain = PlainSoftThreadRepository(session=session)
        assert await plain.find_by_id(plain_id) is not None
        await plain.delete_by_id(plain_id)
        await session.commit()

    assert await _every_row(factory, SoftThread) == 0
    assert await _every_row(factory, SoftThreadPost) == 0


async def test_soft_delete_still_leaves_the_children_alone(relational_backend: RelationalBackend) -> None:
    """``SoftDeleteRepository.delete_by_id`` is an UPDATE of the root: nothing is cascaded or reloaded."""
    factory = await _thread_factory(relational_backend)
    thread_id = await _soft_thread(factory)
    async with factory() as session:
        await SoftThreadRepository(session=session).delete_by_id(thread_id)
        await session.commit()

    assert await _every_row(factory, SoftThread) == 1
    assert await _every_row(factory, SoftThreadPost) == 2
    async with factory() as session:
        assert await session.get(SoftThread, thread_id) is None


async def test_including_deleted_reaches_the_lazy_loads_of_an_object_loaded_before(
    relational_backend: RelationalBackend,
) -> None:
    """An object loaded outside the block carries the criteria to its lazy loads; inside the block they
    see the deleted rows, outside it they do not."""
    factory = await _thread_factory(relational_backend)
    thread_id = await _thread(factory)
    async with factory() as session:
        thread = await session.get(Thread, thread_id)
        assert thread is not None
        with including_deleted():
            bodies = await session.run_sync(lambda _sync: sorted(post.body for post in thread.posts))
        assert bodies == ["dead", "live"]
    async with factory() as session:
        thread = await session.get(Thread, thread_id)
        assert thread is not None
        assert await session.run_sync(lambda _sync: [post.body for post in thread.posts]) == ["live"]
