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
"""Cancellation, commit outcome and retry ordering of ``@transactional``, on a real SQLite file database.

The only cancellation test used to raise ``CancelledError`` from the body against a ``MagicMock`` session
(C162), which hid what a real engine does when Starlette's anyio scope cancels a request mid-transaction
(C062): the rollback is cancelled again, and the pool gets back a leaked or poisoned connection. Every
scenario here runs on the application's own pool (two connections, no overflow) and ends with nothing
checked out and the next call succeeding:

- ``anyio.move_on_after`` around ``@transactional`` while the body awaits something else;
- the same cancel landing while a statement is in flight (the statement is interrupted and its
  connection discarded, not reused);
- two native cancels while the unit completes;
- a driver error raised in place of the cancellation (aiosqlite's ``ValueError('Connection closed')``)
  still ends the call as the cancellation: the anyio scope catches it, ``wait_for`` times out, and the
  unit's own deadline raises ``TransactionTimedOutError`` (WP01-12);
- a commit whose connection fails in flight raises ``CommitOutcomeUnknownError`` (WP01-12), and
  ``@retry`` never retries it, whichever order the two decorators are written in (WP01-17).

The PostgreSQL variants (an in-flight cancel on asyncpg, a double cancel during a rollback held back by a
partitioned network) are in ``tests/integration/test_transaction_manager_matrix.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import (
    CommitOutcomeUnknownError,
    TransactionSynchronizationAdapter,
    TransactionTimedOutError,
    register_synchronization,
)
from pyfly.resilience.retry import retry


class CxItem(Base):
    __tablename__ = "cx_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class CxItemRepository(Repository[CxItem, int]):
    pass


class _CloseTheConnection(TransactionSynchronizationAdapter):
    """Drops the unit's connection once its COMMIT reached the driver: the commit fails in flight.

    The connection closes inside the driver's own commit call. Closed before it, in ``before_commit``, it no
    longer fails the COMMIT on SQLAlchemy 2.1, whose aiosqlite adaptation skips a commit on a closed connection."""

    def __init__(self, repo: CxItemRepository) -> None:
        self._repo = repo

    async def before_commit(self, read_only: bool) -> None:
        connection = await self._repo._session.connection()
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        commit = driver.commit

        async def commit_as_the_connection_drops() -> None:
            await driver.close()
            await commit()

        driver.commit = commit_as_the_connection_drops


class _Gate(TransactionSynchronizationAdapter):
    """Holds the unit at ``before_completion`` until the test releases it."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def before_completion(self) -> None:
        self.reached.set()
        await self.release.wait()


@service
class CxService:
    def __init__(self, items: CxItemRepository) -> None:
        self.items = items
        self.attempts = 0

    @transactional
    async def save_then_wait(self, name: str) -> None:
        await self.items.save(CxItem(name=name))
        await asyncio.sleep(5)

    @transactional
    async def save_then_write_for_long(self, name: str) -> None:
        await self.items.save(CxItem(name=name))
        # A write that would hold SQLite's write lock for many seconds if it were not interrupted.
        await self.items._session.execute(
            text(
                "INSERT INTO cx_item (name) WITH RECURSIVE c(x) AS "
                "(SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 200000000) SELECT 'bulk' FROM c"
            )
        )

    @transactional
    async def place(self, name: str) -> None:
        await self.items.save(CxItem(name=name))

    @transactional
    async def save_then_stand_in_for_the_cancel(self, name: str) -> None:
        await self.items.save(CxItem(name=name))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            # What aiosqlite does once an anyio scope re-cancelled SQLAlchemy's cleanup.
            raise ValueError("Connection closed") from None

    @transactional
    async def save_then_lose_the_cancel(self, name: str) -> None:
        await self.items.save(CxItem(name=name))
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(5)
        raise ValueError("Connection closed")  # raised with no trace of the cancellation it replaced

    @transactional(timeout=0.1)
    async def time_out_behind_a_driver_error(self, name: str) -> None:
        await self.items.save(CxItem(name=name))
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise ValueError("Connection closed") from None

    @transactional
    async def fail_behind_gate(self, gate: _Gate) -> None:
        register_synchronization(gate)
        await self.items.save(CxItem(name="gated"))
        raise ValueError("rolls back through the gate")

    @retry(max_attempts=3, exceptions=(Exception,))
    @transactional
    async def commit_lost_retry_outside(self) -> None:
        self.attempts += 1
        register_synchronization(_CloseTheConnection(self.items))
        await self.items.save(CxItem(name=f"attempt-{self.attempts}"))

    @transactional
    @retry(max_attempts=3, exceptions=(Exception,))
    async def commit_lost_retry_written_inside(self) -> None:
        self.attempts += 1
        register_synchronization(_CloseTheConnection(self.items))
        await self.items.save(CxItem(name=f"attempt-{self.attempts}"))

    @transactional
    @retry(max_attempts=3, exceptions=(ConnectionError,))
    async def flaky_retry_written_inside(self) -> None:
        self.attempts += 1
        await self.items.save(CxItem(name=f"attempt-{self.attempts}"))
        if self.attempts == 1:
            raise ConnectionError("a transient failure after the first write")

    @retry(max_attempts=3, exceptions=(IntegrityError,))
    async def insert_row_one(self) -> None:
        self.attempts += 1
        await self.items._session.execute(text("INSERT INTO cx_item (id, name) VALUES (1, 'row-one')"))

    @transactional
    async def outer_calling_a_retried_step(self) -> None:
        await self.items.save(CxItem(id=1, name="first"))
        await self.insert_row_one()


class App:
    def __init__(self, ctx: ApplicationContext, url: str) -> None:
        self.ctx = ctx
        self.url = url

    @property
    def service(self) -> CxService:
        return self.ctx.get_bean(CxService)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM cx_item ORDER BY id"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cx.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[CxItem.__table__])
    await engine.dispose()
    relational: dict[str, Any] = {
        "enabled": "true",
        "url": url,
        "ddl-auto": "none",
        "pool": {"size": "2", "max-overflow": "0", "timeout": "3"},
    }
    ctx = ApplicationContext(Config({"pyfly": {"data": {"relational": relational}}}))
    for bean in (RelationalAutoConfiguration, CxItemRepository, CxService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield App(ctx, url)
    finally:
        await ctx.stop()


async def test_an_anyio_cancel_while_the_body_waits_leaks_nothing(app: App) -> None:
    for i in range(3):
        with anyio.move_on_after(0.1) as scope:
            await app.service.save_then_wait(f"cancelled-{i}")
        assert scope.cancelled_caught
        assert app.checked_out() == 0
    await app.service.place("next")
    assert await app.committed() == ["next"]
    assert app.checked_out() == 0


async def test_an_anyio_cancel_during_a_statement_interrupts_it_and_discards_the_connection(app: App) -> None:
    for i in range(2):
        with anyio.move_on_after(0.2) as scope:
            await app.service.save_then_write_for_long(f"cancelled-{i}")
        assert scope.cancelled_caught
        assert app.checked_out() == 0
    started = time.perf_counter()
    await app.service.place("next")
    # The interrupted statement released the write lock: no busy_timeout wait (5 s) for the next writer.
    assert time.perf_counter() - started < 2.0
    assert await app.committed() == ["next"]


@pytest.mark.parametrize("method", ["save_then_stand_in_for_the_cancel", "save_then_lose_the_cancel"])
async def test_a_driver_error_in_place_of_an_anyio_cancel_ends_as_the_cancellation(app: App, method: str) -> None:
    with anyio.move_on_after(0.1) as scope:
        await getattr(app.service, method)("cancelled")
    assert scope.cancelled_caught
    assert app.checked_out() == 0
    assert await app.committed() == []
    await app.service.place("next")
    assert await app.committed() == ["next"]


async def test_a_driver_error_in_place_of_a_native_cancel_ends_as_a_timeout(app: App) -> None:
    with pytest.raises(TimeoutError) as raised:
        await asyncio.wait_for(app.service.save_then_stand_in_for_the_cancel("cancelled"), 0.1)
    assert not isinstance(raised.value, TransactionTimedOutError)  # wait_for's own timeout
    assert app.checked_out() == 0
    assert await app.committed() == []


async def test_a_driver_error_in_place_of_the_units_own_deadline_is_a_timeout(app: App) -> None:
    with pytest.raises(TransactionTimedOutError) as raised:
        await app.service.time_out_behind_a_driver_error("late")
    assert isinstance(raised.value.__cause__, ValueError)
    assert app.checked_out() == 0
    assert await app.committed() == []


async def test_two_native_cancels_while_the_unit_completes(app: App) -> None:
    gate = _Gate()
    task = asyncio.create_task(app.service.fail_behind_gate(gate))
    await gate.reached.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.checked_out() == 0
    assert await app.committed() == []
    await app.service.place("next")
    assert await app.committed() == ["next"]


async def test_a_commit_that_fails_in_flight_has_an_unknown_outcome_and_is_not_retried(app: App) -> None:
    with pytest.raises(CommitOutcomeUnknownError) as raised:
        await app.service.commit_lost_retry_outside()
    assert app.service.attempts == 1
    assert raised.value.datasource == "primary"
    assert app.checked_out() == 0
    await app.service.place("next")
    assert await app.committed() == ["next"]


async def test_retry_written_inside_transactional_still_runs_outside_it(app: App) -> None:
    with pytest.raises(CommitOutcomeUnknownError):
        await app.service.commit_lost_retry_written_inside()
    assert app.service.attempts == 1
    app.service.attempts = 0
    await app.service.flaky_retry_written_inside()
    # Each attempt ran in a unit of its own: the failed attempt's write rolled back with it.
    assert await app.committed() == ["attempt-2"]
    assert app.checked_out() == 0


async def test_a_retry_inside_a_rollback_only_unit_stops_at_once(app: App) -> None:
    with pytest.raises(IntegrityError):
        await app.service.outer_calling_a_retried_step()
    assert app.service.attempts == 1  # the unit could no longer commit, so retrying was pointless
    assert await app.committed() == []
    assert app.checked_out() == 0
