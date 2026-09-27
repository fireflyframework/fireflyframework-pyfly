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
"""Every saga step that committed is compensated exactly once (WP13-02, WP13-03, WP13-04: C018, C019, C081).

A real ``ApplicationContext`` runs the saga engine and the relational module on a SQLite file and on
PostgreSQL. Each step writes through a ``@transactional`` service, and a commit gate
(:mod:`tests.support.commit_gate`) holds a real ``COMMIT`` in flight while the test makes the dangerous
thing happen:

- C018: a sibling fails while a step's ``COMMIT`` is in flight. The saga waits for the step instead of
  cancelling it, and compensates it once it committed (it used to cancel it mid-commit, leave it RUNNING and
  never compensate the committed debit).
- C019: the caller cancels the saga while one step commits and another is mid-body. The saga cancels and
  awaits every step task, compensates the step that committed, and re-raises ``CancelledError``; no step task
  outlives it and nothing commits after it (the step tasks used to be orphaned and commit afterwards).
- C081: a step's timeout fires while its ``COMMIT`` is in flight. The step is not retried (a retry committed
  the debit twice) and is compensated; a step whose failed attempt committed nothing is still retried.

After each, no pooled connection is checked out.
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
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.shared.types import StepStatus
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import GATED_LANES, SQLITE_OVERRIDES, CommitGate

pytestmark = pytest.mark.backends(*GATED_LANES)


class SagaLedgerRow(Base):
    __tablename__ = "wp13_saga_ledger"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class SagaLedgerRows(Repository[SagaLedgerRow, int]):
    pass


class ReserveError(Exception):
    """The sibling's business failure."""


class DebitDeclinedError(Exception):
    """A debit attempt that fails before it commits."""


@service
class SagaLedger:
    def __init__(self, rows: SagaLedgerRows) -> None:
        self.rows = rows

    @transactional
    async def record(self, kind: str) -> None:
        await self.rows.save(SagaLedgerRow(kind=kind))

    @transactional
    async def record_then_decline(self, kind: str) -> None:
        await self.rows.save(SagaLedgerRow(kind=kind))
        raise DebitDeclinedError(kind)


class Script:
    """What the steps of the current test do, and what they tell the test."""

    def __init__(self) -> None:
        self.gate: CommitGate | None = None
        self.reserve_failed = asyncio.Event()
        self.credit_waiting = asyncio.Event()
        self.debit_attempts = 0
        self.decline_first_debit = False


@saga(name="wp13-parallel-transfer")
class ParallelTransfer:
    """``debit`` and ``reserve`` in one layer; ``reserve`` fails once ``debit`` is committing."""

    def __init__(self, ledger: SagaLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @saga_step(id="debit", compensate="refund")
    async def debit(self) -> str:
        await self.ledger.record("DEBIT")
        return "debited"

    async def refund(self) -> None:
        await self.ledger.record("REFUND")

    @saga_step(id="reserve")
    async def reserve(self) -> None:
        assert self.script.gate is not None
        await self.script.gate.wait_for_commit()
        self.script.reserve_failed.set()
        raise ReserveError("out of stock")


@saga(name="wp13-cancelled-transfer")
class CancelledTransfer:
    """``debit`` commits behind the gate while ``credit`` waits in its body: the caller cancels the saga."""

    def __init__(self, ledger: SagaLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @saga_step(id="debit", compensate="refund")
    async def debit(self) -> None:
        await self.ledger.record("DEBIT")

    async def refund(self) -> None:
        await self.ledger.record("REFUND")

    @saga_step(id="credit", compensate="uncredit")
    async def credit(self) -> None:
        self.script.credit_waiting.set()
        await asyncio.sleep(30)  # cancelled here, before it writes
        await self.ledger.record("CREDIT")

    async def uncredit(self) -> None:
        await self.ledger.record("UNCREDIT")


@saga(name="wp13-slow-debit")
class SlowDebit:
    """A debit with a timeout and retries."""

    def __init__(self, ledger: SagaLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @saga_step(id="debit", compensate="refund", retry=2, timeout_ms=100)
    async def debit(self) -> None:
        self.script.debit_attempts += 1
        if self.script.decline_first_debit and self.script.debit_attempts == 1:
            await self.ledger.record_then_decline("DEBIT")
        await self.ledger.record("DEBIT")

    async def refund(self) -> None:
        await self.ledger.record("REFUND")


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def engine(self) -> SagaEngine:
        return self.ctx.get_bean(SagaEngine)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, SagaLedgerRow.__tablename__)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT kind FROM {SagaLedgerRow.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(SagaLedgerRow)
    overrides = {"pyfly.transactional.enabled": "true"}
    if relational_backend.lane == SQLITE_FILE:
        overrides.update(SQLITE_OVERRIDES)
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (
        RelationalAutoConfiguration,
        TransactionalEngineAutoConfiguration,
        SagaLedgerRows,
        SagaLedger,
        ParallelTransfer,
        CancelledTransfer,
        SlowDebit,
    ):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        harness = Harness(relational_backend, ctx)
        yield harness
        assert harness.checked_out() == 0
    finally:
        await ctx.stop()


def _live_step_tasks() -> list[str]:
    return sorted(
        task.get_name() for task in asyncio.all_tasks() if task.get_name().startswith("saga-step-") and not task.done()
    )


async def _cancelled_step(name: str) -> None:
    """Wait until the step task *name* is being cancelled (its timeout fired)."""
    for _ in range(1000):
        tasks = [task for task in asyncio.all_tasks() if task.get_name() == name]
        if tasks and tasks[0].cancelling() > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"step task {name} was never cancelled")


async def test_a_sibling_failing_while_a_step_commits_leaves_that_step_compensated(harness: Harness) -> None:
    saga_bean = harness.ctx.get_bean(ParallelTransfer)
    async with harness.gate() as gate:
        saga_bean.script.gate = gate
        await gate.close()
        running = asyncio.create_task(harness.engine.execute("wp13-parallel-transfer"))
        await asyncio.wait_for(saga_bean.script.reserve_failed.wait(), 10)
        await asyncio.sleep(0.05)
        assert not running.done()  # the saga waits for the step whose COMMIT is in flight
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert result.success is False
    assert isinstance(result.error, ReserveError)
    assert result.steps["reserve"].status is StepStatus.FAILED
    assert result.steps["debit"].compensated
    assert result.steps["debit"].status is StepStatus.COMPENSATED
    assert await harness.committed() == ["DEBIT", "REFUND"]
    assert _live_step_tasks() == []


async def test_cancelling_the_saga_awaits_its_steps_and_compensates_the_committed_one(harness: Harness) -> None:
    saga_bean = harness.ctx.get_bean(CancelledTransfer)
    async with harness.gate() as gate:
        await gate.close()
        running = asyncio.create_task(harness.engine.execute("wp13-cancelled-transfer"))
        await gate.wait_for_commit()
        await asyncio.wait_for(saga_bean.script.credit_waiting.wait(), 10)
        running.cancel()
        await asyncio.sleep(0.05)
        assert not running.done()  # it waits for the debit's COMMIT, then compensates
        await gate.open()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 10)

    assert _live_step_tasks() == []
    assert await harness.committed() == ["DEBIT", "REFUND"]
    await asyncio.sleep(0.2)
    assert await harness.committed() == ["DEBIT", "REFUND"]  # nothing commits after the saga ended


async def test_cancelling_the_saga_mid_body_commits_nothing_and_leaves_no_step_running(harness: Harness) -> None:
    saga_bean = harness.ctx.get_bean(CancelledTransfer)
    running = asyncio.create_task(harness.engine.execute("wp13-cancelled-transfer"))
    await asyncio.wait_for(saga_bean.script.credit_waiting.wait(), 10)
    for _ in range(500):  # the debit commits on its own: wait for it, then cancel the waiting credit
        if await harness.committed():
            break
        await asyncio.sleep(0.01)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 10)

    assert _live_step_tasks() == []
    assert await harness.committed() == ["DEBIT", "REFUND"]


async def test_a_timeout_during_commit_is_never_retried_and_the_step_is_compensated(harness: Harness) -> None:
    saga_bean = harness.ctx.get_bean(SlowDebit)
    async with harness.gate() as gate:
        await gate.close()
        running = asyncio.create_task(harness.engine.execute("wp13-slow-debit"))
        await gate.wait_for_commit()
        await _cancelled_step("saga-step-debit")  # the 100 ms timeout fired while COMMIT waits
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert saga_bean.script.debit_attempts == 1  # a retry would have committed a second debit
    assert result.success is False
    assert isinstance(result.error, TimeoutError)
    assert result.steps["debit"].compensated
    assert await harness.committed() == ["DEBIT", "REFUND"]


async def test_an_attempt_that_committed_nothing_is_still_retried(harness: Harness) -> None:
    saga_bean = harness.ctx.get_bean(SlowDebit)
    saga_bean.script.decline_first_debit = True
    result = await harness.engine.execute("wp13-slow-debit")

    assert result.success is True
    assert saga_bean.script.debit_attempts == 2
    assert result.steps["debit"].attempts == 2
    assert await harness.committed() == ["DEBIT"]
