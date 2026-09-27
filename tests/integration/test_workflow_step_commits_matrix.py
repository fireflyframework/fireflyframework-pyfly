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
"""A workflow compensates every step that committed, after every sibling settled (WP13-01, WP13-04, WP13-11).

A real ``ApplicationContext`` runs the workflow engine and the relational module on a SQLite file and on
PostgreSQL. Steps write through a ``@transactional`` service, and a commit gate
(:mod:`tests.support.commit_gate`) holds a real ``COMMIT`` in flight.

- C017: when a step of a parallel layer fails, its siblings are cancelled and awaited before compensation
  runs. A sibling whose ``COMMIT`` was in flight commits (commits are shielded) and is compensated; a sibling
  still in its body rolls back, and nothing commits after the workflow returned FAILED (the sibling used to
  commit afterwards, never compensated, while the persisted state said RUNNING).
- C081: a step whose timeout fires while its ``COMMIT`` is in flight is not retried (a retry committed it
  twice) and is compensated; an attempt that committed nothing is still retried.
- C082: stopping the context drains the ASYNC workflow runs in flight, so a run started just before shutdown
  (by a one-shot shell command, say) completes instead of being abandoned.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

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
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.transactional.auto_configuration import TransactionalEngineAutoConfiguration
from pyfly.transactional.core.model import ExecutionStatus, StepStatus, TriggerMode
from pyfly.transactional.workflow.annotations import compensation_step, workflow, workflow_step
from pyfly.transactional.workflow.engine import WorkflowEngine
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import GATED_LANES, SQLITE_OVERRIDES, CommitGate

pytestmark = pytest.mark.backends(*GATED_LANES)


class WorkflowLedgerRow(Base):
    __tablename__ = "wp13_workflow_ledger"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class WorkflowLedgerRows(Repository[WorkflowLedgerRow, int]):
    pass


class ChargeError(Exception):
    """The failing sibling's business failure."""


class DeclinedError(Exception):
    """A step attempt that fails before it commits."""


@service
class WorkflowLedger:
    def __init__(self, rows: WorkflowLedgerRows) -> None:
        self.rows = rows

    @transactional
    async def record(self, kind: str) -> None:
        await self.rows.save(WorkflowLedgerRow(kind=kind))

    @transactional
    async def record_then_decline(self, kind: str) -> None:
        await self.rows.save(WorkflowLedgerRow(kind=kind))
        raise DeclinedError(kind)


class Script:
    def __init__(self) -> None:
        self.gate: CommitGate | None = None
        self.charge_failed = asyncio.Event()
        self.reserve_waiting = asyncio.Event()
        self.hold_reserve = False
        self.reserve_attempts = 0
        self.decline_first_reserve = False


@workflow(id="wp13-parallel-order")
class ParallelOrder:
    """``reserve`` and ``charge`` in one layer; ``charge`` fails."""

    def __init__(self, ledger: WorkflowLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @workflow_step(id="reserve", compensatable=True)
    async def reserve(self) -> None:
        if self.script.hold_reserve:
            self.script.reserve_waiting.set()
            await asyncio.sleep(30)  # cancelled here, before it writes
        await self.ledger.record("RESERVED")

    @compensation_step(for_step="reserve")
    async def release(self) -> None:
        await self.ledger.record("RELEASED")

    @workflow_step(id="charge")
    async def charge(self) -> None:
        if self.script.gate is not None:
            await self.script.gate.wait_for_commit()
        else:
            await asyncio.wait_for(self.script.reserve_waiting.wait(), 10)
        self.script.charge_failed.set()
        raise ChargeError("card declined")


@workflow(id="wp13-slow-reserve")
class SlowReserve:
    def __init__(self, ledger: WorkflowLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @workflow_step(id="reserve", compensatable=True, timeout_ms=100, max_retries=1)
    async def reserve(self) -> None:
        self.script.reserve_attempts += 1
        if self.script.decline_first_reserve and self.script.reserve_attempts == 1:
            await self.ledger.record_then_decline("RESERVED")
        await self.ledger.record("RESERVED")

    @compensation_step(for_step="reserve")
    async def release(self) -> None:
        await self.ledger.record("RELEASED")


@workflow(id="wp13-async-import", trigger_mode=TriggerMode.ASYNC)
class AsyncImport:
    def __init__(self, ledger: WorkflowLedger) -> None:
        self.ledger = ledger

    @workflow_step(id="load")
    async def load(self) -> None:
        await asyncio.sleep(0.2)
        await self.ledger.record("IMPORTED")


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        await self.ctx.stop()

    @property
    def engine(self) -> WorkflowEngine:
        return self.ctx.get_bean(WorkflowEngine)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, WorkflowLedgerRow.__tablename__)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT kind FROM {WorkflowLedgerRow.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(WorkflowLedgerRow)
    overrides = {"pyfly.transactional.enabled": "true"}
    if relational_backend.lane == SQLITE_FILE:
        overrides.update(SQLITE_OVERRIDES)
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (
        RelationalAutoConfiguration,
        TransactionalEngineAutoConfiguration,
        WorkflowLedgerRows,
        WorkflowLedger,
        ParallelOrder,
        SlowReserve,
        AsyncImport,
    ):
        ctx.register_bean(bean)
    await ctx.start()
    harness = Harness(relational_backend, ctx)
    try:
        yield harness
        if not harness.stopped:
            assert harness.checked_out() == 0
    finally:
        if not harness.stopped:
            await ctx.stop()


async def _cancelled_step(name: str) -> None:
    """Wait until the step task *name* is being cancelled (its timeout fired)."""
    for _ in range(1000):
        tasks = [task for task in asyncio.all_tasks() if task.get_name() == name]
        if tasks and tasks[0].cancelling() > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"step task {name} was never cancelled")


async def test_a_sibling_committing_when_a_step_fails_is_awaited_and_compensated(harness: Harness) -> None:
    bean = harness.ctx.get_bean(ParallelOrder)
    async with harness.gate() as gate:
        bean.script.gate = gate
        await gate.close()
        running = asyncio.create_task(harness.engine.start("wp13-parallel-order"))
        await asyncio.wait_for(bean.script.charge_failed.wait(), 10)
        await asyncio.sleep(0.05)
        assert not running.done()  # compensation waits until the committing sibling settled
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert result.status is ExecutionStatus.FAILED
    assert await harness.committed() == ["RESERVED", "RELEASED"]
    state = await harness.engine.get_execution(result.correlation_id)
    assert state is not None
    assert state.payload["steps"]["reserve"]["status"] == StepStatus.COMPENSATED.value


async def test_a_sibling_still_in_its_body_when_a_step_fails_commits_nothing(harness: Harness) -> None:
    bean = harness.ctx.get_bean(ParallelOrder)
    bean.script.hold_reserve = True
    result = await asyncio.wait_for(harness.engine.start("wp13-parallel-order"), 10)

    assert result.status is ExecutionStatus.FAILED
    await asyncio.sleep(0.2)
    assert await harness.committed() == []  # the cancelled sibling rolled back and never committed later
    state = await harness.engine.get_execution(result.correlation_id)
    assert state is not None
    assert state.payload["steps"]["reserve"]["status"] != StepStatus.RUNNING.value


async def test_a_timeout_during_commit_is_never_retried_and_the_step_is_compensated(harness: Harness) -> None:
    bean = harness.ctx.get_bean(SlowReserve)
    async with harness.gate() as gate:
        await gate.close()
        running = asyncio.create_task(harness.engine.start("wp13-slow-reserve"))
        await gate.wait_for_commit()
        await _cancelled_step("workflow-step-reserve")
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert bean.script.reserve_attempts == 1  # a retry would have committed it twice
    assert result.status is ExecutionStatus.FAILED
    assert await harness.committed() == ["RESERVED", "RELEASED"]


async def test_an_attempt_that_committed_nothing_is_still_retried(harness: Harness) -> None:
    bean = harness.ctx.get_bean(SlowReserve)
    bean.script.decline_first_reserve = True
    result = await harness.engine.start("wp13-slow-reserve")

    assert result.status is ExecutionStatus.COMPLETED
    assert bean.script.reserve_attempts == 2
    assert await harness.committed() == ["RESERVED"]


async def test_stopping_the_context_drains_an_async_workflow_run(harness: Harness) -> None:
    started = await harness.engine.start("wp13-async-import")
    assert started.status is ExecutionStatus.PENDING
    engine = harness.engine
    await harness.stop()  # a one-shot command returned right after starting it

    assert await harness.committed() == ["IMPORTED"]
    state = await engine.get_execution(started.correlation_id)
    assert state is not None
    assert state.status is ExecutionStatus.COMPLETED
