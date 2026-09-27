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
"""Every TCC participant whose TRY committed is cancelled (WP13-03, WP13-04: C019, C081).

A real ``ApplicationContext`` runs the TCC engine and the relational module on a SQLite file and on
PostgreSQL. Each phase writes through a ``@transactional`` service; a commit gate
(:mod:`tests.support.commit_gate`) holds a real ``COMMIT`` in flight.

- C081: a TRY whose timeout fires while its ``COMMIT`` is in flight is not retried (a retry reserved twice)
  and takes part in the CANCEL phase (it used to count as failed, so CANCEL skipped it and its reservation
  leaked). A TRY whose failed attempt committed nothing is still retried.
- An optional participant whose TRY timed out after committing is skipped, and cancelled at once so its
  reservation does not leak while the TCC goes on and confirms the others.
- C019: a caller that cancels the TCC while a TRY runs gets ``CancelledError`` once the CANCEL phase has
  released what the TRYs before it reserved (CANCEL never ran on cancellation).
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
from pyfly.transactional.tcc.annotations import cancel_method, confirm_method, tcc, tcc_participant, try_method
from pyfly.transactional.tcc.core.phase import TccPhase
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import GATED_LANES, SQLITE_OVERRIDES, CommitGate

pytestmark = pytest.mark.backends(*GATED_LANES)


class TccLedgerRow(Base):
    __tablename__ = "wp13_tcc_ledger"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))


@repository
class TccLedgerRows(Repository[TccLedgerRow, int]):
    pass


class ReservationDeclinedError(Exception):
    """A TRY attempt that fails before it commits."""


@service
class TccLedger:
    def __init__(self, rows: TccLedgerRows) -> None:
        self.rows = rows

    @transactional
    async def record(self, kind: str) -> None:
        await self.rows.save(TccLedgerRow(kind=kind))

    @transactional
    async def record_then_decline(self, kind: str) -> None:
        await self.rows.save(TccLedgerRow(kind=kind))
        raise ReservationDeclinedError(kind)


class Script:
    def __init__(self) -> None:
        self.try_attempts = 0
        self.decline_first_try = False
        self.second_try_waiting = asyncio.Event()


@tcc(name="wp13-slow-reservation")
class SlowReservation:
    """One participant whose TRY has a timeout and one retry."""

    def __init__(self, ledger: TccLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @tcc_participant(id="stock", order=1)
    class Stock:
        @try_method(timeout_ms=1000, retry=1)
        async def reserve(self) -> str:
            self.script.try_attempts += 1
            if self.script.decline_first_try and self.script.try_attempts == 1:
                await self.ledger.record_then_decline("RESERVED")
            await self.ledger.record("RESERVED")
            return "reserved"

        @confirm_method()
        async def confirm(self) -> None:
            await self.ledger.record("CONFIRMED")

        @cancel_method()
        async def release(self) -> None:
            await self.ledger.record("RELEASED")


@tcc(name="wp13-optional-points")
class OptionalPoints:
    """An optional loyalty participant whose TRY times out while it commits, then a required one."""

    def __init__(self, ledger: TccLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @tcc_participant(id="loyalty", order=1, optional=True)
    class Loyalty:
        @try_method(timeout_ms=1000)
        async def award(self) -> None:
            await self.ledger.record("POINTS")

        @confirm_method()
        async def keep(self) -> None:
            await self.ledger.record("POINTS_KEPT")

        @cancel_method()
        async def revoke(self) -> None:
            await self.ledger.record("POINTS_REVOKED")

    @tcc_participant(id="stock", order=2)
    class Stock:
        @try_method()
        async def reserve(self) -> None:
            await self.ledger.record("RESERVED")

        @confirm_method()
        async def confirm(self) -> None:
            await self.ledger.record("CONFIRMED")

        @cancel_method()
        async def release(self) -> None:
            await self.ledger.record("RELEASED")


@tcc(name="wp13-cancelled-reservation")
class CancelledReservation:
    """``stock`` reserves, then the caller cancels the TCC while ``payment`` tries."""

    def __init__(self, ledger: TccLedger) -> None:
        self.ledger = ledger
        self.script = Script()

    @tcc_participant(id="stock", order=1)
    class Stock:
        @try_method()
        async def reserve(self) -> None:
            await self.ledger.record("RESERVED")

        @confirm_method()
        async def confirm(self) -> None:
            await self.ledger.record("CONFIRMED")

        @cancel_method()
        async def release(self) -> None:
            await self.ledger.record("RELEASED")

    @tcc_participant(id="payment", order=2)
    class Payment:
        @try_method()
        async def hold(self) -> None:
            self.script.second_try_waiting.set()
            await asyncio.sleep(30)  # cancelled here
            await self.ledger.record("HELD")

        @confirm_method()
        async def capture(self) -> None:
            await self.ledger.record("CAPTURED")

        @cancel_method()
        async def unhold(self) -> None:
            await self.ledger.record("UNHELD")


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def engine(self) -> TccEngine:
        return self.ctx.get_bean(TccEngine)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, TccLedgerRow.__tablename__)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT kind FROM {TccLedgerRow.__tablename__} ORDER BY id"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(TccLedgerRow)
    overrides = {"pyfly.transactional.enabled": "true"}
    if relational_backend.lane == SQLITE_FILE:
        overrides.update(SQLITE_OVERRIDES)
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (
        RelationalAutoConfiguration,
        TransactionalEngineAutoConfiguration,
        TccLedgerRows,
        TccLedger,
        SlowReservation,
        OptionalPoints,
        CancelledReservation,
    ):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        harness = Harness(relational_backend, ctx)
        yield harness
        assert harness.checked_out() == 0
    finally:
        await ctx.stop()


async def _timed_out(owner: asyncio.Task[object]) -> None:
    """Wait until *owner* (the task running the TCC, which runs its TRYs) is being cancelled by a timeout."""
    for _ in range(1000):
        if owner.cancelling() > 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the TRY never timed out")


async def test_a_try_timing_out_during_commit_is_not_retried_and_is_cancelled(harness: Harness) -> None:
    bean = harness.ctx.get_bean(SlowReservation)
    async with harness.gate() as gate:
        await gate.close()
        running: asyncio.Task[object] = asyncio.create_task(harness.engine.execute("wp13-slow-reservation"))
        await gate.wait_for_commit()
        await _timed_out(running)  # the 1 s timeout fired while COMMIT waits
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert bean.script.try_attempts == 1  # a retry would have reserved twice
    assert result.success is False  # type: ignore[attr-defined]
    assert result.final_phase is TccPhase.CANCEL  # type: ignore[attr-defined]
    assert await harness.committed() == ["RESERVED", "RELEASED"]


async def test_a_try_attempt_that_committed_nothing_is_still_retried(harness: Harness) -> None:
    bean = harness.ctx.get_bean(SlowReservation)
    bean.script.decline_first_try = True
    result = await harness.engine.execute("wp13-slow-reservation")

    assert result.success is True
    assert bean.script.try_attempts == 2
    assert await harness.committed() == ["RESERVED", "CONFIRMED"]


async def test_cancelling_the_tcc_cancels_the_participants_that_tried(harness: Harness) -> None:
    bean = harness.ctx.get_bean(CancelledReservation)
    running = asyncio.create_task(harness.engine.execute("wp13-cancelled-reservation"))
    await asyncio.wait_for(bean.script.second_try_waiting.wait(), 10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 10)

    assert await harness.committed() == ["RESERVED", "RELEASED"]


async def test_an_optional_try_that_committed_as_it_timed_out_is_cancelled_at_once(harness: Harness) -> None:
    async with harness.gate() as gate:
        await gate.close()
        running: asyncio.Task[object] = asyncio.create_task(harness.engine.execute("wp13-optional-points"))
        await gate.wait_for_commit()
        await _timed_out(running)
        await gate.open()
        result = await asyncio.wait_for(running, 10)

    assert result.success is True  # type: ignore[attr-defined]
    assert await harness.committed() == ["POINTS", "POINTS_REVOKED", "RESERVED", "CONFIRMED"]
