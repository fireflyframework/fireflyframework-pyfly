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
"""``track_commits()`` tells whether a block committed anything (WP13, the base of C018, C019 and C081).

An orchestration engine runs step code it does not control, and must know whether a step that failed, timed
out or was cancelled committed work first: such a step is compensated and never retried blindly, and a step
that rolled back is neither. Here, on a SQLite file and on PostgreSQL, with real units of work:

- a ``@transactional`` call that commits counts as committed, and one that raises as rolled back;
- a repository write outside a transaction (its write auto unit) counts; a read, a read-only unit and a
  participant that joined an enclosing unit do not (the enclosing unit's boundary is what commits);
- a unit whose ``COMMIT`` was in flight when its task was cancelled counts as committed, although the call
  raised ``CancelledError`` (the commit is shielded and lands);
- units opened by tasks started inside the block count, and units of ``detached()`` work do not;
- nested blocks both see a unit of the inner one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import CommitTracker, detached, track_commits
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import SQLITE_OVERRIDES, CommitGate

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class TrackedRow(Base):
    __tablename__ = "commit_tracking_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64))


@repository
class TrackedRows(Repository[TrackedRow, int]):
    pass


class RolledBackError(Exception):
    """The business failure a unit rolls back on."""


@service
class TrackedWriter:
    def __init__(self, rows: TrackedRows) -> None:
        self.rows = rows

    @transactional
    async def write(self, row_id: int) -> None:
        await self.rows.save(TrackedRow(id=row_id, name="written"))

    @transactional
    async def write_then_fail(self, row_id: int) -> None:
        await self.rows.save(TrackedRow(id=row_id, name="rolled back"))
        raise RolledBackError(str(row_id))

    @transactional(read_only=True)
    async def read(self) -> int:
        return len(await self.rows.find_all())

    @transactional
    async def write_twice_in_one_unit(self, first: int, second: int) -> None:
        await self.write(first)  # joins this unit
        await self.write(second)


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def writer(self) -> TrackedWriter:
        return self.ctx.get_bean(TrackedWriter)

    @property
    def rows(self) -> TrackedRows:
        return self.ctx.get_bean(TrackedRows)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, TrackedRow.__tablename__)

    async def committed(self) -> list[int]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT id FROM {TrackedRow.__tablename__} ORDER BY id"))
                return [int(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(TrackedRow)
    overrides = SQLITE_OVERRIDES if relational_backend.lane == SQLITE_FILE else {}
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (RelationalAutoConfiguration, TrackedRows, TrackedWriter):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield Harness(relational_backend, ctx)
    finally:
        await ctx.stop()


def _counts(tracker: CommitTracker) -> tuple[int, int, int]:
    return tracker.committed, tracker.rolled_back, tracker.unknown


async def test_a_committed_unit_counts_as_committed(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.writer.write(1)
    assert _counts(commits) == (1, 0, 0)
    assert commits.may_have_committed


async def test_a_unit_that_raises_counts_as_rolled_back(harness: Harness) -> None:
    with track_commits() as commits, pytest.raises(RolledBackError):
        await harness.writer.write_then_fail(1)
    assert _counts(commits) == (0, 1, 0)
    assert not commits.may_have_committed
    assert await harness.committed() == []


async def test_a_repository_write_outside_a_transaction_counts_and_a_read_does_not(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.rows.save(TrackedRow(id=1, name="auto unit"))
        await harness.rows.find_all()
        await harness.writer.read()
    assert _counts(commits) == (1, 0, 0)


async def test_a_participant_is_not_counted_apart_from_the_unit_it_joined(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.writer.write_twice_in_one_unit(1, 2)
    assert _counts(commits) == (1, 0, 0)
    assert await harness.committed() == [1, 2]


async def test_a_commit_in_flight_when_the_task_is_cancelled_counts_as_committed(harness: Harness) -> None:
    trackers: list[CommitTracker] = []

    async def step() -> None:
        with track_commits() as commits:
            trackers.append(commits)
            await harness.writer.write(7)

    async with harness.gate() as gate:
        await gate.close()
        task = asyncio.create_task(step())
        await gate.wait_for_commit()
        task.cancel()
        await asyncio.sleep(0.05)  # the cancellation lands while the COMMIT waits behind the gate
        assert not task.done()  # the commit is shielded: the task waits for it
        await gate.open()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _counts(trackers[0]) == (1, 0, 0)
    assert await harness.committed() == [7]


async def test_child_tasks_count_and_detached_work_does_not(harness: Harness) -> None:
    detached_done = asyncio.Event()

    async def detached_write() -> None:
        await harness.writer.write(3)
        detached_done.set()

    with track_commits() as commits:
        await asyncio.gather(harness.writer.write(1), harness.writer.write(2))
        await detached(detached_write())
    await asyncio.wait_for(detached_done.wait(), 5)
    assert _counts(commits) == (2, 0, 0)
    assert await harness.committed() == [1, 2, 3]


async def test_nested_blocks_both_see_a_unit_of_the_inner_one(harness: Harness) -> None:
    with track_commits() as outer:
        await harness.writer.write(1)
        with track_commits() as inner:
            await harness.writer.write(2)
    assert _counts(outer) == (2, 0, 0)
    assert _counts(inner) == (1, 0, 0)


async def test_a_unit_outside_every_block_reports_to_no_tracker(harness: Harness) -> None:
    with track_commits() as commits:
        pass
    await harness.writer.write(1)
    assert _counts(commits) == (0, 0, 0)
