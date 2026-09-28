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
"""Scheduled ``@transactional`` jobs on a real database (WP13-06, WP13-07, WP13-08).

A real ``ApplicationContext`` runs the scheduling and relational auto-configurations on a SQLite file and on
PostgreSQL:

- C071: a ``fixed_rate`` job slower than its period never overlaps itself, so it holds one pooled
  connection, never one per period, and every run commits (overlapping runs used to exhaust the pool on
  PostgreSQL and fail with "database is locked" on SQLite);
- C151: ``ctx.stop()`` while a ``fixed_delay`` batch is in flight lets the batch commit, as it does for the
  other triggers (it used to be cancelled and rolled back at once);
- C152: with the lease lock (``pyfly.scheduling.lock.provider=database``) failing at run time, the failure
  is logged with the job's name and the tick is skipped, and nothing escapes as an unretrieved task
  exception;
- a run that outlives its lock's TTL is cancelled, and its unit of work rolls back; when it ends after the
  job's next run took the lease, its late release leaves that run's lease alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import locks
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration
from pyfly.scheduling.decorators import scheduled
from pyfly.scheduling.lock import DistributedLock
from pyfly.scheduling.task_scheduler import TaskScheduler
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class JobRow(Base):
    __tablename__ = "wp13_job_row"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class JobRows(Repository[JobRow, int]):
    pass


class Probe:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.runs = 0
        self.started = asyncio.Event()

    def enter(self) -> None:
        self.runs += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()

    def leave(self) -> None:
        self.in_flight -= 1


@service
class Poller:
    """Slower than its period: 0.15 s of work every 0.05 s."""

    def __init__(self, rows: JobRows) -> None:
        self.rows = rows
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=0.05))
    @transactional
    async def poll(self) -> None:
        self.probe.enter()
        try:
            await self.rows.save(JobRow(kind="poll"))
            await asyncio.sleep(0.15)  # inside the unit: the run holds its connection
        finally:
            self.probe.leave()


@service
class Batch:
    """A long fixed_delay batch in two parts."""

    def __init__(self, rows: JobRows) -> None:
        self.rows = rows
        self.probe = Probe()

    @scheduled(fixed_delay=timedelta(seconds=30))
    @transactional
    async def process(self) -> None:
        self.probe.enter()
        try:
            await self.rows.save(JobRow(kind="part-1"))
            await asyncio.sleep(0.3)
            await self.rows.save(JobRow(kind="part-2"))
        finally:
            self.probe.leave()


@service
class LockedReport:
    def __init__(self, rows: JobRows) -> None:
        self.rows = rows
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=0.05), lock="report")
    @transactional
    async def report(self) -> None:
        self.probe.enter()
        try:
            await self.rows.save(JobRow(kind="report"))
        finally:
            self.probe.leave()


@service
class Overrun:
    """Its run outlives its lock's 0.2 s ttl."""

    def __init__(self, rows: JobRows) -> None:
        self.rows = rows
        self.probe = Probe()

    @scheduled(fixed_rate=timedelta(seconds=30), lock="overrun", lock_ttl=timedelta(seconds=0.2))
    @transactional
    async def overrun(self) -> None:
        self.probe.enter()
        try:
            await self.rows.save(JobRow(kind="overrun"))
            await asyncio.sleep(5)
        finally:
            self.probe.leave()


@service
class LateEnder:
    """Not scheduled: the test runs it through the scheduler. Its first run is cancelled at its lock's ttl and
    takes a while to end (as when its ``COMMIT`` is in flight); its second run holds the lease until told to
    end, then saves a row.

    It is not ``@transactional``: on SQLite a write unit takes the database's one write lock at its start, so a
    second run could not even take the lease (a write too) while the first one's unit is ending.
    """

    def __init__(self, rows: JobRows) -> None:
        self.rows = rows
        self.runs: list[str] = []
        self.first_cancelled = asyncio.Event()
        self.first_may_end = asyncio.Event()
        self.second_holds = asyncio.Event()
        self.second_may_end = asyncio.Event()

    async def work(self) -> None:
        kind = f"run-{len(self.runs) + 1}"
        self.runs.append(kind)
        if kind == "run-1":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.first_cancelled.set()
                await asyncio.shield(self.first_may_end.wait())
                raise
        elif kind == "run-2":
            self.second_holds.set()
            await self.second_may_end.wait()
        await self.rows.save(JobRow(kind=kind))


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        await self.ctx.stop()

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT kind FROM {JobRow.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()

    async def execute(self, sql: str) -> None:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()


def _context(backend: RelationalBackend, job: type, overrides: dict[str, Any] | None = None) -> ApplicationContext:
    ctx = ApplicationContext(backend.config({"pyfly.data.relational.pool.size": "5", **(overrides or {})}))
    for bean in (RelationalAutoConfiguration, SchedulingAutoConfiguration, JobRows, job):
        ctx.register_bean(bean)
    return ctx


@pytest.fixture
async def start(relational_backend: RelationalBackend) -> AsyncIterator[Callable[..., Any]]:
    await relational_backend.create_tables(JobRow)
    await relational_backend.create_tables(locks)
    harnesses: list[Harness] = []

    async def _start(job: type, overrides: dict[str, Any] | None = None) -> Harness:
        ctx = _context(relational_backend, job, overrides)
        await ctx.start()
        harness = Harness(relational_backend, ctx)
        harnesses.append(harness)
        return harness

    try:
        yield _start
    finally:
        for harness in harnesses:
            if not harness.stopped:
                await harness.ctx.stop()


async def test_a_slow_fixed_rate_job_never_overlaps_and_every_run_commits(start: Callable[..., Any]) -> None:
    harness: Harness = await start(Poller)
    poller = harness.ctx.get_bean(Poller)
    engine = harness.ctx.get_bean(DataSourceRegistry).primary.engine
    checked_out: list[int] = []
    for _ in range(40):
        checked_out.append(engine.sync_engine.pool.checkedout())
        await asyncio.sleep(0.02)
    await harness.stop()

    assert poller.probe.max_in_flight == 1
    assert max(checked_out) <= 1  # one run, one connection, whatever the period
    committed = await harness.committed()
    assert len(committed) == poller.probe.runs >= 3  # every run committed, none failed on a lock


async def test_stopping_the_context_lets_a_fixed_delay_batch_in_flight_commit(start: Callable[..., Any]) -> None:
    harness: Harness = await start(Batch)
    batch = harness.ctx.get_bean(Batch)
    await asyncio.wait_for(batch.probe.started.wait(), 10)
    await harness.stop()

    assert await harness.committed() == ["part-1", "part-2"]


async def test_a_lock_that_fails_at_run_time_is_logged_with_the_job_name(
    start: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    harness: Harness = await start(LockedReport, {"pyfly.scheduling.lock.provider": "database"})
    report = harness.ctx.get_bean(LockedReport)
    await asyncio.wait_for(report.probe.started.wait(), 10)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
            await harness.execute("DROP TABLE pyfly_locks")  # the lock's table is gone: every acquire fails
            runs = report.probe.runs
            for _ in range(400):
                if any("acquiring lock" in r.getMessage() for r in caplog.records):
                    break
                await asyncio.sleep(0.01)
            await harness.stop()
    finally:
        loop.set_exception_handler(None)

    failures = [r for r in caplog.records if "acquiring lock" in r.getMessage()]
    assert failures and "LockedReport.report" in failures[0].getMessage()
    assert report.probe.runs <= runs + 1  # ticks skipped (one may have taken the lock before the drop)
    assert unhandled == []


async def test_a_run_that_outlives_its_lock_ttl_is_cancelled_and_rolls_back(
    start: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="pyfly.scheduling.task_scheduler"):
        harness: Harness = await start(Overrun, {"pyfly.scheduling.lock.provider": "database"})
        overrun = harness.ctx.get_bean(Overrun)
        await asyncio.wait_for(overrun.probe.started.wait(), 10)
        for _ in range(300):
            if overrun.probe.in_flight == 0:
                break
            await asyncio.sleep(0.01)
        assert overrun.probe.in_flight == 0  # cancelled at the ttl, long before its 5 s of work
        await harness.stop()

    assert await harness.committed() == []
    assert any("ran past the ttl" in r.getMessage() and "Overrun.overrun" in r.getMessage() for r in caplog.records)


async def test_the_late_release_of_a_run_cancelled_at_its_ttl_leaves_the_next_runs_lease(
    start: Callable[..., Any],
) -> None:
    """The lease lock tells a process's acquisitions apart by the task that took them: the release must come
    from the run's own task, or it ends the lease of the run that took it since, and the job overlaps itself."""
    harness: Harness = await start(LateEnder, {"pyfly.scheduling.lock.provider": "database"})
    job = harness.ctx.get_bean(LateEnder)
    scheduler = harness.ctx.get_bean(TaskScheduler)
    lock = harness.ctx.get_bean(DistributedLock)  # type: ignore[type-abstract]
    assert isinstance(lock, LeaseLock)

    first = asyncio.create_task(scheduler._invoke(job, job.work, lock="late", lock_ttl=0.3))
    await asyncio.wait_for(job.first_cancelled.wait(), 10)  # its lease ended at the ttl; the run is still ending
    second = asyncio.create_task(scheduler._invoke(job, job.work, lock="late", lock_ttl=30.0))
    await asyncio.wait_for(job.second_holds.wait(), 10)  # the next run took the lease
    holder = await lock.holder("late")
    job.first_may_end.set()
    await asyncio.wait_for(first, 10)  # the first run ends now, and releases its lease late

    assert await lock.holder("late") == holder  # the second run keeps its lease
    # skipped: the second run holds it
    await asyncio.wait_for(scheduler._invoke(job, job.work, lock="late", lock_ttl=30.0), 10)
    job.second_may_end.set()
    await asyncio.wait_for(second, 10)
    await asyncio.wait_for(harness.stop(), 30)

    assert job.runs == ["run-1", "run-2"]
    assert await harness.committed() == ["run-2"]
