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
"""``@async_method`` runs in its own unit of work and never blocks its caller (WP13-05, C032).

``@async_method`` used to await the method inline: it ran in the caller's transaction, blocked the caller for
its whole duration, and its failure rolled the caller back, while a caller failure after the call discarded
the "asynchronous" write. On a SQLite file and on PostgreSQL, with a real ``ApplicationContext``:

- the caller returns at once with the task running the method, which runs with its own unit of work;
- a failure of the method does not reach the caller (the order commits) and goes to the
  ``AsyncUncaughtExceptionHandler`` bean (by default, the log);
- a caller that fails after the call rolls back alone: the audit row commits in its own transaction;
- awaiting the returned task gives the method's result, and a synchronous method runs off the loop;
- ``ctx.stop()`` waits for the calls in flight before any bean they use is destroyed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import component, repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import current_unit_of_work
from pyfly.scheduling.async_methods import AsyncUncaughtExceptionHandler
from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration
from pyfly.scheduling.decorators import async_method
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class OrderRow(Base):
    __tablename__ = "wp13_async_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))


@repository
class OrderRows(Repository[OrderRow, int]):
    pass


class WebhookDownError(Exception):
    """The notifier's failure."""


class CallerFailedError(Exception):
    """The caller's failure after the call."""


@service
class Notifier:
    def __init__(self, rows: OrderRows) -> None:
        self.rows = rows
        self.units: list[object] = []
        self.threads: list[str] = []

    @async_method
    @transactional
    async def audit(self, order: str, *, fail: bool = False) -> str:
        self.units.append(current_unit_of_work())
        await asyncio.sleep(0.3)
        await self.rows.save(OrderRow(kind=f"audit-{order}"))
        if fail:
            raise WebhookDownError(order)
        return f"audited {order}"

    @async_method
    def render(self, order: str) -> str:
        self.threads.append(threading.current_thread().name)
        return f"rendered {order}"


@service
class Orders:
    def __init__(self, rows: OrderRows, notifier: Notifier) -> None:
        self.rows = rows
        self.notifier = notifier
        self.units: list[object] = []
        self.calls: list[Any] = []

    @transactional
    async def place(self, order: str, *, notifier_fails: bool = False, caller_fails: bool = False) -> None:
        self.units.append(current_unit_of_work())
        await self.rows.save(OrderRow(kind=f"order-{order}"))
        self.calls.append(await self.notifier.audit(order, fail=notifier_fails))
        if caller_fails:
            raise CallerFailedError(order)


@component
class RecordingHandler(AsyncUncaughtExceptionHandler):
    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def handle_uncaught_exception(
        self, error: BaseException, method: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.seen.append((type(error).__name__, getattr(method, "__name__", "?")))


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
                rows = await conn.execute(text(f"SELECT kind FROM {OrderRow.__tablename__} ORDER BY kind"))
                return [str(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(OrderRow)
    ctx = ApplicationContext(relational_backend.config())
    beans = (RelationalAutoConfiguration, SchedulingAutoConfiguration, OrderRows, Notifier, Orders, RecordingHandler)
    for bean in beans:
        ctx.register_bean(bean)
    await ctx.start()
    harness = Harness(relational_backend, ctx)
    try:
        yield harness
    finally:
        if not harness.stopped:
            await ctx.stop()


async def test_the_caller_returns_at_once_and_the_method_runs_in_its_own_unit(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)
    notifier = harness.ctx.get_bean(Notifier)
    started = time.monotonic()
    await orders.place("o0")
    assert time.monotonic() - started < 0.25  # not blocked for the audit's 0.3 s

    task = orders.calls[0]
    assert isinstance(task, asyncio.Task)
    assert await asyncio.wait_for(task, 10) == "audited o0"
    assert notifier.units[0] is not None and notifier.units[0] is not orders.units[0]
    assert await harness.committed() == ["audit-o0", "order-o0"]


async def test_a_failing_method_leaves_the_caller_committed_and_reaches_the_handler(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)
    await orders.place("o1", notifier_fails=True)  # the webhook failure never reaches the caller
    with pytest.raises(WebhookDownError):
        await asyncio.wait_for(orders.calls[0], 10)

    handler = harness.ctx.get_bean(RecordingHandler)
    assert handler.seen == [("WebhookDownError", "audit")]
    assert await harness.committed() == ["order-o1"]  # the audit rolled back alone


async def test_a_caller_failing_after_the_call_rolls_back_alone(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)
    with pytest.raises(CallerFailedError):
        await orders.place("o2", caller_fails=True)
    assert await asyncio.wait_for(orders.calls[0], 10) == "audited o2"

    assert await harness.committed() == ["audit-o2"]  # committed in its own transaction


async def test_a_synchronous_method_runs_off_the_event_loop(harness: Harness) -> None:
    notifier = harness.ctx.get_bean(Notifier)
    task = await notifier.render("o3")
    assert await asyncio.wait_for(task, 10) == "rendered o3"
    assert notifier.threads and notifier.threads[0] != threading.main_thread().name


async def test_stopping_the_context_waits_for_the_calls_in_flight(harness: Harness) -> None:
    orders = harness.ctx.get_bean(Orders)
    await orders.place("o4")
    await harness.stop()

    assert await harness.committed() == ["audit-o4", "order-o4"]
