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

from typing import Any

import pytest
from sqlalchemy import Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.data.relational.sqlalchemy import Base, Repository
from pyfly.data.relational.sqlalchemy.session import SessionProvider
from pyfly.data.transaction import Isolation, Propagation, transactional
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
