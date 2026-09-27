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
"""A cancellation at every point of a unit's life (WP01-12, WP01-13).

The other cancellation tests cancel while the body sleeps or while one multi-second statement runs. Here
the cancel point sweeps across a whole ``@transactional`` unit made of short statements: ``BEGIN`` (``BEGIN
IMMEDIATE`` on SQLite), the flush's ``INSERT``, the refresh's ``SELECT``, a ``count()``, a second flush and
``COMMIT``. Each step cancels one call, through an anyio cancel scope (level-triggered: every await of
the cancelled task is cancelled again, SQLAlchemy's own cleanup included, as under Starlette) or through
``asyncio.wait_for`` (a single native cancel), and then checks that:

- the call completed, or ended as the cancellation (``cancelled_caught``, ``TimeoutError``), never with
  a driver error that took its place (aiosqlite's ``ValueError('Connection closed')``, asyncmy's
  ``InterfaceError('Cancelled during execution')``);
- no pooled connection is left checked out;
- on SQLite, another connection takes the write lock at once, with the garbage collector off: a
  discarded connection never leaves a half-closed handle holding the lock.

At the end, PostgreSQL has no backend idle in transaction, and the next unit commits.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterator

import anyio
import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.engine import make_url
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
from tests.support.backend_matrix import MYSQL, PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG, MYSQL)

STEPS = 60
"""Cancel points per sweep, spread from the first await to past the end of the unit."""


class SwItem(Base):
    __tablename__ = "sw_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class SwItemRepository(Repository[SwItem, int]):
    pass


@service
class SwService:
    def __init__(self, items: SwItemRepository) -> None:
        self.items = items

    @transactional
    async def whole_unit(self, name: str) -> None:
        await self.items.save(SwItem(name=f"{name}/a"))  # BEGIN, INSERT (flush), SELECT (refresh)
        await self.items.count()  # SELECT
        await self.items.save(SwItem(name=f"{name}/b"))  # INSERT, SELECT; COMMIT on exit


class Sweep:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def service(self) -> SwService:
        return self.ctx.get_bean(SwService)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    def write_lock_free(self) -> bool:
        """Whether another connection can take SQLite's write lock right now (always true on a server)."""
        if not self.backend.is_embedded:
            return True
        probe = sqlite3.connect(str(make_url(self.backend.url).database), timeout=0.2)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            probe.close()

    async def idle_in_transaction(self) -> int:
        if self.backend.dialect != "postgresql":
            return 0
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                sql = (
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
                return int((await conn.execute(text(sql))).scalar_one())
        finally:
            await engine.dispose()

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM sw_item ORDER BY id"))).all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def sweep(relational_backend: RelationalBackend) -> AsyncIterator[Sweep]:
    await relational_backend.create_tables(SwItem)
    ctx = ApplicationContext(relational_backend.config({"pyfly.data.relational.pool.size": "3"}))
    for bean in (RelationalAutoConfiguration, SwItemRepository, SwService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield Sweep(relational_backend, ctx)
    finally:
        await asyncio.wait_for(ctx.stop(), 30)


async def _duration_of_one_unit(sweep: Sweep) -> float:
    """The longest of a few units, the first on a new connection: a cancelled unit discards its connection,
    so most units of the sweep start on a new one."""
    durations = []
    for i in range(5):
        started = time.perf_counter()
        await sweep.service.whole_unit(f"warm-{i}")
        durations.append(time.perf_counter() - started)
    return max(durations)


async def _cancelled_call(sweep: Sweep, kind: str, delay: float, name: str) -> str:
    """Run one unit cancelled after *delay* seconds; return ``"completed"`` or ``"cancelled"``."""
    if kind == "anyio":
        with anyio.move_on_after(delay) as scope:
            await sweep.service.whole_unit(name)
        return "cancelled" if scope.cancelled_caught else "completed"
    try:
        await asyncio.wait_for(sweep.service.whole_unit(name), delay)
    except TimeoutError:
        return "cancelled"
    return "completed"


@pytest.mark.parametrize("kind", ["anyio", "wait_for"])
async def test_a_cancel_anywhere_in_a_unit_ends_as_the_cancellation_and_leaves_nothing_behind(
    sweep: Sweep, kind: str
) -> None:
    full = await _duration_of_one_unit(sweep)
    outcomes: Counter[str] = Counter()
    collecting = gc.isenabled()
    gc.disable()  # a half-closed SQLite handle holds the lock until the collector runs: never let it help
    try:
        for step in range(STEPS):
            delay = full * 1.3 * step / STEPS
            where = f"{kind} step {step} ({delay * 1000:.3f} ms of a {full * 1000:.3f} ms unit)"
            try:
                outcomes[await _cancelled_call(sweep, kind, delay, f"{kind}-{step}")] += 1
            except Exception as error:  # noqa: BLE001 — the assertion reports it
                pytest.fail(f"{where}: ended with {error!r} instead of completing or being cancelled")
            assert sweep.checked_out() == 0, f"{where}: a pooled connection is still checked out"
            assert sweep.write_lock_free(), f"{where}: SQLite's write lock is still held"
    finally:
        if collecting:
            gc.enable()
    assert outcomes["cancelled"] > 0, outcomes
    assert outcomes["completed"] > 0, outcomes
    assert await sweep.idle_in_transaction() == 0
    await sweep.service.whole_unit("after")
    committed = await sweep.committed()
    assert committed[-2:] == ["after/a", "after/b"]
    # A unit either committed both rows or none: a cancelled unit never commits half of its work.
    rows_per_unit: Counter[str] = Counter(name.split("/")[0] for name in committed)
    assert set(rows_per_unit.values()) == {2}, rows_per_unit
    assert sweep.checked_out() == 0
