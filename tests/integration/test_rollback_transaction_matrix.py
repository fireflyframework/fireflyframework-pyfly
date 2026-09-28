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
"""A test's units of work roll back when it ends, on every backend of the matrix (C156).

``data_slice(..., rollback=True)`` (and ``@DataTest``) run every unit of work of the test (repository
calls, ``@transactional`` services, ``SessionProvider`` units) as a savepoint of one transaction per
datasource, which rolls back when the test ends. Each unit still behaves as it does in production: it
commits (releases its savepoint) or rolls back on its own, so a failed unit leaves the test's earlier
writes in place, even on PostgreSQL, where a failed statement ends the whole transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from sqlalchemy import Integer, String, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration, repository, service
from pyfly.data.query import query
from pyfly.data.relational.sqlalchemy import Base, Repository
from pyfly.data.relational.sqlalchemy.repository import STREAM_FIRST_BATCH
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Isolation,
    Propagation,
    UnexpectedRollbackError,
    transactional,
)
from pyfly.kernel.exceptions import DuplicateKeyException
from pyfly.testing import data_slice
from tests.support.backend_matrix import RelationalBackend


class _RollbackNote(Base):
    __tablename__ = "wp11_rollback_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(50), unique=True)


@repository
class NoteRepository(Repository[_RollbackNote, int]):
    async def find_by_body(self, body: str) -> _RollbackNote | None: ...


@repository
class MissingTableRepository(Repository[_RollbackNote, int]):
    @query("SELECT count(*) FROM wp11_rollback_missing", native=True)
    async def count_missing(self) -> int: ...


@service
class NoteService:
    def __init__(self, notes: NoteRepository) -> None:
        self._notes = notes

    @transactional
    async def add(self, body: str) -> None:
        await self._notes.save(_RollbackNote(body=body))

    @transactional
    async def add_then_fail(self, body: str) -> None:
        await self._notes.save(_RollbackNote(body=body))
        raise ValueError("the unit fails after its write")

    @transactional(propagation=Propagation.NESTED)
    async def add_nested(self, body: str, *, fail: bool = False) -> None:
        await self._notes.save(_RollbackNote(body=body))
        if fail:
            raise ValueError("the nested step fails")

    @transactional
    async def add_with_a_failed_step(self, body: str) -> None:
        await self._notes.save(_RollbackNote(body=body))
        with pytest.raises(ValueError):
            await self.add_nested(f"{body}-nested", fail=True)

    @transactional(read_only=True, isolation=Isolation.SERIALIZABLE)
    async def bodies(self) -> list[str]:
        return sorted(note.body for note in await self._notes.find_all())


@service
class ParentChildService:
    """A parent unit that starts a child task with a unit of its own, and writes on while the child's unit is
    open, instead of waiting for it."""

    def __init__(self, notes: NoteRepository) -> None:
        self._notes = notes
        self.child_started = asyncio.Event()

    @transactional
    async def write_beside_a_failing_child(self) -> None:
        await self._notes.save(_RollbackNote(body="parent-1"))
        child = asyncio.create_task(self.child_fails_later())
        await self.child_started.wait()
        await self._notes.save(_RollbackNote(body="parent-2"))  # the child's unit is still open
        with pytest.raises(ValueError, match="the child fails"):
            await child

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def child_fails_later(self) -> None:
        await self._notes.save(_RollbackNote(body="child"))
        self.child_started.set()
        await asyncio.sleep(0.2)
        raise ValueError("the child fails")


async def _committed(backend: RelationalBackend) -> int:
    """The rows another connection sees: only what was committed."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return int((await connection.execute(text("SELECT count(*) FROM wp11_rollback_note"))).scalar() or 0)
    finally:
        await engine.dispose()


def _slice(backend: RelationalBackend, **options: Any) -> Any:
    return data_slice(NoteRepository, NoteService, config=backend.config(), rollback=True, **options)


async def test_every_unit_of_a_test_rolls_back_when_it_ends(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(_RollbackNote)
    async with await _slice(relational_backend) as context:
        notes = context.get_bean(NoteRepository)
        notes_service = context.get_bean(NoteService)
        await notes.save(_RollbackNote(body="auto-unit"))  # a repository call outside a transaction
        await notes_service.add("transactional")
        with pytest.raises(ValueError):
            await notes_service.add_then_fail("failed")  # its own savepoint rolls back, the rest stays
        async with context.get_bean(SessionProvider).unit() as session:
            session.add(_RollbackNote(body="session-provider"))
        assert await notes_service.bodies() == ["auto-unit", "session-provider", "transactional"]
        assert (await notes.find_by_body("auto-unit")) is not None
        assert await _committed(relational_backend) == 0  # nothing is committed while the test runs
    assert await _committed(relational_backend) == 0


async def test_a_test_does_not_see_the_rows_of_the_test_before(relational_backend: RelationalBackend) -> None:
    """The same unique row, written by two tests in turn: the second one starts from an empty table."""
    await relational_backend.create_tables(_RollbackNote)
    for _test in range(2):
        async with await _slice(relational_backend) as context:
            notes = context.get_bean(NoteRepository)
            assert await notes.count() == 0
            await notes.save(_RollbackNote(body="unique"))
            assert await notes.count() == 1


async def test_a_failed_statement_leaves_the_test_transaction_usable(relational_backend: RelationalBackend) -> None:
    """A duplicate key fails its unit only: on PostgreSQL too, the test goes on writing."""
    await relational_backend.create_tables(_RollbackNote)
    async with await _slice(relational_backend) as context:
        notes_service = context.get_bean(NoteService)
        await notes_service.add("duplicate")
        with pytest.raises(DuplicateKeyException):
            await notes_service.add("duplicate")
        await notes_service.add("after")
        assert await notes_service.bodies() == ["after", "duplicate"]


async def test_nested_steps_roll_back_to_their_savepoint(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(_RollbackNote)
    async with await _slice(relational_backend) as context:
        notes_service = context.get_bean(NoteService)
        await notes_service.add_with_a_failed_step("outer")
        assert await notes_service.bodies() == ["outer"]


@pytest.mark.parametrize("rollback", [False, True], ids=["production", "rollback"])
async def test_what_a_loop_writes_while_it_reads_a_stream_stays(
    relational_backend: RelationalBackend, rollback: bool
) -> None:
    """A stream read outside a transaction keeps its read auto unit open while the loop body runs, and the units
    the loop completes meanwhile get savepoints inside that unit's. The read unit's end releases its savepoint:
    rolling it back, as a read unit ends in production, undid every copy the loop saved, with no error.

    More rows than a stream's first batch: the stream is read on the test's connection, which the loop's units
    share, so it reads its rows when it opens. With its cursor open, the loop's statements hung the connection
    on MySQL and MariaDB, and on SQLite the scan went on into the copies the loop had just written."""
    seeds = STREAM_FIRST_BATCH + 2
    await relational_backend.create_tables(_RollbackNote)
    async with await data_slice(NoteRepository, config=relational_backend.config(), rollback=rollback) as context:
        notes = context.get_bean(NoteRepository)
        for number in range(seeds):
            await notes.save(_RollbackNote(body=f"seed-{number}"))
        copied = 0
        async with contextlib.aclosing(notes.stream_all()) as stream:
            async for note in stream:
                await notes.save(_RollbackNote(body=f"copy-{note.body}"))
                copied += 1
        assert copied == seeds
        assert await notes.count() == 2 * seeds
        with pytest.raises(ValueError, match="the loop stops"):  # a loop that fails keeps what it wrote before
            async with contextlib.aclosing(notes.stream_all()) as stream:
                async for note in stream:
                    await notes.save(_RollbackNote(body=f"again-{note.body}"))
                    raise ValueError("the loop stops")
        assert await notes.count() == 2 * seeds + 1
    assert await _committed(relational_backend) == (0 if rollback else 2 * seeds + 1)


async def test_a_failed_read_leaves_the_test_transaction_usable(relational_backend: RelationalBackend) -> None:
    """A read unit whose statement fails rolls back to its savepoint (a read unit that ends well releases it): on
    PostgreSQL, where the failure aborts the transaction, the test goes on writing."""
    await relational_backend.create_tables(_RollbackNote)
    async with await data_slice(
        NoteRepository, MissingTableRepository, config=relational_backend.config(), rollback=True
    ) as context:
        notes = context.get_bean(NoteRepository)
        await notes.save(_RollbackNote(body="before"))
        with pytest.raises(DBAPIError):
            await context.get_bean(MissingTableRepository).count_missing()
        await notes.save(_RollbackNote(body="after"))
        assert sorted(note.body for note in await notes.find_all()) == ["after", "before"]
    assert await _committed(relational_backend) == 0


@pytest.mark.backends("pg", "mysql", "mariadb")
@pytest.mark.parametrize("rollback", [False, True], ids=["production", "rollback"])
async def test_a_parent_that_writes_inside_a_childs_unit_fails_instead_of_losing_the_write(
    relational_backend: RelationalBackend, rollback: bool
) -> None:
    """The child's unit starts inside the parent's (the parent's task started it), but the parent writes on
    instead of waiting: on the test's one connection that write runs inside the child's savepoint, and the
    child's rollback undid it, with no error. The parent's unit is marked rollback-only instead, and its
    commit fails naming why; in production each unit has a connection of its own."""
    await relational_backend.create_tables(_RollbackNote)
    async with await data_slice(
        NoteRepository, ParentChildService, config=relational_backend.config(), rollback=rollback
    ) as context:
        notes = context.get_bean(NoteRepository)
        family = context.get_bean(ParentChildService)
        if not rollback:
            await family.write_beside_a_failing_child()
            assert sorted(note.body for note in await notes.find_all()) == ["parent-1", "parent-2"]
            return
        with pytest.raises(UnexpectedRollbackError) as failure:
            await family.write_beside_a_failing_child()
        assert isinstance(failure.value.__cause__, IllegalTransactionStateError)
        assert "inside that unit's savepoint" in str(failure.value.__cause__)
        assert await notes.count() == 0  # the parent rolled back whole, as the child did
        await notes.save(_RollbackNote(body="after"))  # and the test's transaction goes on
        assert await notes.count() == 1


async def test_the_original_transaction_managers_are_back_after_the_test(
    relational_backend: RelationalBackend,
) -> None:
    """Outside the rollback transaction, the context's units commit again."""
    from pyfly.testing import RollbackTransaction

    await relational_backend.create_tables(_RollbackNote)
    async with await data_slice(NoteRepository, config=relational_backend.config()) as context:
        notes = context.get_bean(NoteRepository)
        async with RollbackTransaction(context):
            await notes.save(_RollbackNote(body="rolled-back"))
        assert await _committed(relational_backend) == 0
        await notes.save(_RollbackNote(body="committed"))
        assert await _committed(relational_backend) == 1


_APPLICATION_ENGINE: dict[str, AsyncEngine] = {}


@configuration
class _ApplicationEngine:
    """An application that brings its own engine (it replaces the primary one)."""

    @bean
    def async_engine(self) -> AsyncEngine:
        return _APPLICATION_ENGINE["engine"]


@pytest.mark.parametrize("engine_options", [{}, {"isolation_level": "AUTOCOMMIT"}], ids=["plain", "autocommit"])
async def test_the_application_engine_rolls_back_too(
    relational_backend: RelationalBackend, engine_options: dict[str, str]
) -> None:
    """An application's own engine, a plain one or one that runs in AUTOCOMMIT: the test's connection holds a
    real transaction (on SQLite, and in AUTOCOMMIT, nothing would send its BEGIN), so the first unit's
    RELEASE SAVEPOINT commits nothing."""
    await relational_backend.create_tables(_RollbackNote)
    _APPLICATION_ENGINE["engine"] = relational_backend.create_engine(**engine_options)
    async with await data_slice(
        NoteRepository, NoteService, _ApplicationEngine, config=relational_backend.config(), rollback=True
    ) as context:
        assert context.get_bean(AsyncEngine) is _APPLICATION_ENGINE["engine"]
        await context.get_bean(NoteService).add("transactional")
        await context.get_bean(NoteRepository).save(_RollbackNote(body="auto-unit"))
        assert await context.get_bean(NoteRepository).count() == 2
        assert await _committed(relational_backend) == 0
    assert await _committed(relational_backend) == 0


class _Unregistered:
    pass


@service
class _NeedsUnregistered:
    def __init__(self, missing: _Unregistered) -> None:
        self._missing = missing


@pytest.mark.backends("pg", "mysql")
async def test_a_slice_that_fails_fast_leaves_no_connection_open(relational_backend: RelationalBackend) -> None:
    """C176: a slice whose fail-fast check fails is stopped: its registry is closed, and the server holds none of
    its connections."""
    from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    await relational_backend.create_tables(_RollbackNote)
    for _attempt in range(3):
        config = relational_backend.config()
        registry = DataSourceRegistry.for_config(config)
        async with registry.primary.engine.connect():
            pass  # the slice's pool holds a connection, as after a store checked its table
        with pytest.raises((NoSuchBeanError, BeanCreationException)):
            await data_slice(NoteRepository, _NeedsUnregistered, config=config)
        assert registry.closed
    assert await _connections(relational_backend) == 0


async def _connections(backend: RelationalBackend) -> int:
    """The connections to the test's database, other than the one counting them."""
    from sqlalchemy.engine import make_url

    database = make_url(backend.url).database
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            if backend.dialect == "postgresql":
                sql = "SELECT count(*) FROM pg_stat_activity WHERE datname = :db AND pid <> pg_backend_pid()"
            else:
                sql = "SELECT count(*) FROM information_schema.processlist WHERE db = :db AND id <> CONNECTION_ID()"
            return int((await connection.execute(text(sql), {"db": database})).scalar() or 0)
    finally:
        await engine.dispose()
