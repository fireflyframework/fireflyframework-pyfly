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
"""A background workflow run started inside a unit of work starts once that unit commits (WP13).

An ASYNC workflow (and ``WorkflowEngine.start_async``) saves its ``PENDING`` state in the caller's task and
runs in a detached task. Started inside the caller's ``@transactional``, the run used to start at once:

- with the cache provider (which writes at the commit), a run that ended before the caller committed had
  its ``COMPLETED`` state overwritten by the caller's ``PENDING``, which then stayed forever (the recovery
  scan reported it as stale, and cleanup never removed it);
- with every provider, a caller that rolled back still ran the workflow, whose business writes and state
  were committed although the order that started it never was.

A real ``ApplicationContext`` runs the workflow engine and the relational module on a SQLite file and on
PostgreSQL, with the SQL, the cache and the in-memory persistence providers. The run now starts after the
caller's commit, and never when it rolls back; outside a unit it starts at once, as before. The same holds for
``start_async`` called from a ``@transactional`` workflow step, whose unit is the step's own. A unit that
commits while the engine drains, or whose commit outcome is unknown (PostgreSQL: its backend is killed during
``COMMIT``), does not start the run either, and its ``PENDING`` state is kept for the recovery scan.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.cache.auto_configuration import CacheAutoConfiguration
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import orchestration_state_table
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import CommitOutcomeUnknownError
from pyfly.transactional.auto_configuration import TransactionalEngineAutoConfiguration
from pyfly.transactional.core.model import ExecutionStatus, TriggerMode
from pyfly.transactional.core.persistence import ExecutionState
from pyfly.transactional.workflow.annotations import workflow, workflow_step
from pyfly.transactional.workflow.engine import WorkflowEngine
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import CommitGate

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

PROVIDERS = ("sqlalchemy", "cache", "memory")

OPEN_UNIT_WAIT_S = 0.5
"""How long the caller keeps its unit open after the start: a run that started at once ends well within it."""


class AsyncStartRow(Base):
    __tablename__ = "wp13_async_start_ledger"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class AsyncStartRows(Repository[AsyncStartRow, int]):
    pass


class PlacementError(Exception):
    """The caller's business failure, which rolls its unit back."""


@workflow(id="wp13-async-ship", trigger_mode=TriggerMode.ASYNC)
class AsyncShip:
    """The ASYNC workflow a ``@transactional`` caller starts.

    Its step writes nothing to the database: on the SQLite file, a write would wait for the caller's write lock,
    and so hide a run that started before the caller committed.
    """

    def __init__(self) -> None:
        self.ran = asyncio.Event()

    @workflow_step(id="ship")
    async def ship(self) -> str:
        self.ran.set()
        return "shipped"


@workflow(id="wp13-child-invoice")
class ChildInvoice:
    """The child a ``@transactional`` step starts with ``start_async`` (no database write either)."""

    def __init__(self) -> None:
        self.ran = asyncio.Event()

    @workflow_step(id="invoice")
    async def invoice(self) -> str:
        self.ran.set()
        return "invoiced"


async def _ended_meanwhile(engine: WorkflowEngine, correlation_id: str) -> bool:
    """Keep the caller's unit open for :data:`OPEN_UNIT_WAIT_S`, or until the run's state says it ended."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OPEN_UNIT_WAIT_S
    while loop.time() < deadline:
        state = await engine.get_execution(correlation_id)
        if state is not None and state.status.is_terminal:
            return True
        await asyncio.sleep(0.02)
    return False


@service
class Orders:
    """Places an order and starts its ASYNC fulfilment in the same unit of work."""

    def __init__(self, rows: AsyncStartRows, engine: WorkflowEngine, ship: AsyncShip) -> None:
        self.rows = rows
        self.engine = engine
        self.ship = ship
        self.correlation_id = ""
        self.ended_in_unit = False
        self.ran_in_unit = False
        self.before_return: Callable[[], Awaitable[None]] | None = None

    @transactional
    async def place(self, *, fail: bool = False) -> str:
        await self.rows.save(AsyncStartRow(kind="ORDER"))
        started = await self.engine.start("wp13-async-ship")
        assert started.status is ExecutionStatus.PENDING
        self.correlation_id = started.correlation_id
        self.ended_in_unit = await _ended_meanwhile(self.engine, started.correlation_id)
        self.ran_in_unit = self.ship.ran.is_set()
        if fail:
            raise PlacementError("payment declined")
        if self.before_return is not None:
            await self.before_return()
        return started.correlation_id


@workflow(id="wp13-spawn-invoice")
class SpawnInvoice:
    """A synchronous workflow whose ``@transactional`` step starts a child with ``start_async``."""

    def __init__(self, rows: AsyncStartRows, engine: WorkflowEngine, child: ChildInvoice) -> None:
        self.rows = rows
        self.engine = engine
        self.child = child
        self.fail = False
        self.child_correlation_id = ""
        self.ended_in_unit = False
        self.ran_in_unit = False

    @workflow_step(id="bill")
    @transactional
    async def bill(self) -> str:
        await self.rows.save(AsyncStartRow(kind="BILLED"))
        started = await self.engine.start_async("wp13-child-invoice")
        self.child_correlation_id = started.correlation_id
        self.ended_in_unit = await _ended_meanwhile(self.engine, started.correlation_id)
        self.ran_in_unit = self.child.ran.is_set()
        if self.fail:
            raise PlacementError("billing refused")
        return started.correlation_id


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext, provider: str) -> None:
        self.backend = backend
        self.ctx = ctx
        self.provider = provider

    @property
    def engine(self) -> WorkflowEngine:
        return self.ctx.get_bean(WorkflowEngine)

    async def settle(self) -> None:
        """Wait for every background run in flight, then accept new ones again."""
        await asyncio.wait_for(self.engine.drain(), 10)
        self.engine.accept_background_runs()

    async def final_state(self, correlation_id: str) -> ExecutionState | None:
        """The run's state once every background run ended."""
        await self.settle()
        return await self.engine.get_execution(correlation_id)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, AsyncStartRow.__tablename__)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def all_connections_returned(self, timeout: float = 5.0) -> int:
        """The checked-out count once it reaches zero, or after *timeout* seconds.

        A failed step's rollback runs shielded and can return its connection a few loop turns after the
        run reports its outcome; a leaked connection never comes back, so it still fails the check.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while (count := self.checked_out()) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        return count

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT kind FROM {AsyncStartRow.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture(params=PROVIDERS)
async def harness(request: pytest.FixtureRequest, relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    provider = str(request.param)
    await relational_backend.create_tables(AsyncStartRow)
    await relational_backend.create_tables(orchestration_state_table())
    overrides = {
        "pyfly.transactional.enabled": "true",
        "pyfly.transactional.persistence.provider": provider,
    }
    beans: list[type] = [RelationalAutoConfiguration]
    if provider == "cache":
        overrides.update({"pyfly.cache.enabled": "true", "pyfly.cache.provider": "memory"})
        beans.append(CacheAutoConfiguration)  # before the engine's configuration, which needs its CacheAdapter
    beans.append(TransactionalEngineAutoConfiguration)
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (*beans, AsyncStartRows, AsyncShip, ChildInvoice, Orders, SpawnInvoice):
        ctx.register_bean(bean)
    await ctx.start()
    harness = Harness(relational_backend, ctx, provider)
    try:
        yield harness
        await harness.settle()
        assert await harness.all_connections_returned() == 0
    finally:
        await ctx.stop()


async def test_a_run_started_in_a_unit_starts_once_the_caller_commits_and_ends_completed(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)

    correlation_id = await asyncio.wait_for(orders.place(), 10)

    state = await harness.final_state(correlation_id)
    assert state is not None
    assert state.status is ExecutionStatus.COMPLETED  # not the caller's PENDING, written after the run ended
    assert not orders.ran_in_unit  # the run waited for the caller's commit
    assert not orders.ended_in_unit
    assert await harness.committed() == ["ORDER"]


async def test_a_run_started_in_a_unit_that_rolls_back_never_starts_and_leaves_no_state(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)

    with pytest.raises(PlacementError):
        await asyncio.wait_for(orders.place(fail=True), 10)

    assert await harness.final_state(orders.correlation_id) is None
    assert not harness.ctx.get_bean(AsyncShip).ran.is_set()
    assert await harness.committed() == []


async def test_a_run_started_outside_a_unit_starts_at_once(harness: Harness) -> None:
    started = await harness.engine.start("wp13-async-ship")

    assert started.status is ExecutionStatus.PENDING
    await asyncio.wait_for(harness.ctx.get_bean(AsyncShip).ran.wait(), 10)  # nothing to wait for
    state = await harness.final_state(started.correlation_id)
    assert state is not None
    assert state.status is ExecutionStatus.COMPLETED


async def test_a_child_started_by_a_transactional_step_starts_once_the_step_commits(harness: Harness) -> None:
    parent = harness.ctx.get_bean(SpawnInvoice)

    result = await asyncio.wait_for(harness.engine.start("wp13-spawn-invoice"), 10)

    assert result.status is ExecutionStatus.COMPLETED
    state = await harness.final_state(parent.child_correlation_id)
    assert state is not None
    assert state.status is ExecutionStatus.COMPLETED
    assert not parent.ran_in_unit  # the child waited for the step's commit
    assert not parent.ended_in_unit
    assert await harness.committed() == ["BILLED"]


async def test_a_child_started_by_a_transactional_step_that_rolls_back_never_starts(harness: Harness) -> None:
    parent = harness.ctx.get_bean(SpawnInvoice)
    parent.fail = True

    result = await asyncio.wait_for(harness.engine.start("wp13-spawn-invoice"), 10)

    assert result.status is ExecutionStatus.FAILED
    assert await harness.final_state(parent.child_correlation_id) is None
    assert not harness.ctx.get_bean(ChildInvoice).ran.is_set()
    assert await harness.committed() == []


async def test_a_run_whose_caller_commits_while_the_engine_drains_is_not_started(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    orders = harness.ctx.get_bean(Orders)
    orders.before_return = harness.engine.drain  # the context starts stopping before the caller commits

    with caplog.at_level(logging.WARNING, logger="pyfly.transactional.workflow.engine"):
        correlation_id = await asyncio.wait_for(orders.place(), 10)
    harness.engine.accept_background_runs()

    await harness.settle()
    assert not harness.ctx.get_bean(AsyncShip).ran.is_set()
    state = await harness.engine.get_execution(correlation_id)
    assert state is not None
    assert state.status is ExecutionStatus.PENDING  # left for the recovery scan to report
    assert any(correlation_id in record.getMessage() and "stopping" in record.getMessage() for record in caplog.records)
    assert await harness.committed() == ["ORDER"]


@pytest.mark.backends(PG)
async def test_a_run_whose_callers_commit_outcome_is_unknown_is_not_started(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    orders = harness.ctx.get_bean(Orders)
    async with harness.gate() as gate:
        await gate.close()
        with caplog.at_level(logging.WARNING, logger="pyfly.transactional.workflow.engine"):
            placing = asyncio.create_task(orders.place())
            await gate.wait_for_commit()
            # The committing backend waits on the gate's advisory lock: kill it while its COMMIT is in flight.
            admin = create_async_engine(harness.backend.url, poolclass=NullPool)
            killed: list[object] = []
            try:
                async with admin.connect() as conn:
                    for _ in range(500):
                        rows = await conn.execute(
                            text(
                                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = "
                                "current_database() AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                            )
                        )
                        killed = list(rows.all())
                        if killed:
                            break
                        await asyncio.sleep(0.01)
            finally:
                await admin.dispose()
            assert killed
            with pytest.raises(CommitOutcomeUnknownError):
                await asyncio.wait_for(placing, 10)
        await gate.open()

    await harness.settle()
    assert not harness.ctx.get_bean(AsyncShip).ran.is_set()
    assert any(
        orders.correlation_id in record.getMessage() and "unknown" in record.getMessage() for record in caplog.records
    )
    state = await harness.engine.get_execution(orders.correlation_id)
    if harness.provider == "memory":  # written at once, and kept: the unit may have committed
        assert state is not None and state.status is ExecutionStatus.PENDING
    else:  # the server rolled the killed COMMIT back, and the cache provider never wrote it
        assert state is None
    assert await harness.committed() == []
