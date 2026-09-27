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
"""A streamed result and the other statements of its unit, on every backend (WP01-05).

``stream_all`` reads through a server-side cursor on the unit's one connection. PostgreSQL (asyncpg keeps
the cursor a portal of the transaction) and SQLite run other statements on that connection while the
cursor is open, so a statement between two fetches, from the stream's own task or from a sibling in
``gather()``, just works there. A MySQL or MariaDB connection has one active result at a time: a statement
sent while an unbuffered result is open corrupts the connection under asyncmy (``Packet sequence number
wrong``, a rollback that fails, a MariaDB connection that hangs). The unit refuses that statement instead,
with ``IllegalTransactionStateError`` naming the stream, before anything reaches the server; the stream
itself goes on, and the unit stays usable. A stream that is exhausted, or closed early
(``contextlib.aclosing``), frees the connection; one abandoned open (a ``break`` out of the loop) is
closed when its unit completes, so the unit still commits or rolls back cleanly. A cancellation anywhere in
a stream (a fetch in flight, the close of a stream ended early) ends as the cancellation and leaves no
connection behind.

The fast suite runs the single-result rules on SQLite too, with the capability turned off
(``TransactionCapabilities.multiple_active_results``): see ``test_unit_of_work_streams_single_result``.

Every body runs under ``asyncio.wait_for``: before the refusal existed a MariaDB run hung, and a MySQL run
hung past its own ``wait_for`` in the rollback of the corrupted connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable
from typing import TypeVar

import anyio
import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import IllegalTransactionStateError
from tests.support.backend_matrix import MARIADB, MYSQL, PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG, MYSQL, MARIADB)

T = TypeVar("T")

ROWS = 300
BOUND = 20.0  # seconds: a hang fails the test instead of holding the suite


class StItem(Base):
    __tablename__ = "st_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


@repository
class StItemRepository(Repository[StItem, int]):
    async def stream_slowly(self, fetched: asyncio.Event) -> int:
        """A stream whose every row takes the server 0.1 s (MySQL and MariaDB ``SLEEP``), closed on the way out.
        Each row outgrows the server's network buffer, so it is sent as soon as it is computed."""
        result = await self._session.stream(
            text("SELECT id, REPEAT('x', 40000) AS pad, SLEEP(0.1) FROM st_item ORDER BY id")
        )
        streamed = 0
        try:
            async for _row in result:
                streamed += 1
                fetched.set()
        finally:
            await result.close()
        return streamed

    async def first_ten_in_a_savepoint_block(self) -> int:
        """The savepoint idiom around a stream left open (a break out of the loop) as the block ends."""
        session = self._session
        streamed = 0
        async with session.begin_nested():
            await self.save(StItem(name="in-the-block"))
            async for _item in self.stream_all():
                streamed += 1
                if streamed == 10:
                    break
        return streamed


@service
class StService:
    def __init__(self, items: StItemRepository) -> None:
        self.items = items

    @transactional
    async def seed(self) -> None:
        await self.items.save_all([StItem(name=f"s-{i:03d}") for i in range(ROWS)])

    @transactional
    async def count_while_streaming(self) -> tuple[int, list[int]]:
        streamed, counts = 0, []
        async for _item in self.items.stream_all():
            streamed += 1
            if streamed % 100 == 0:
                counts.append(await self.items.count())  # a statement between two fetches, same task
        return streamed, counts

    @transactional
    async def write_beside_a_stream(self) -> list[object]:
        streaming, attempted = asyncio.Event(), asyncio.Event()

        async def consume() -> int:
            streamed = 0
            async for _item in self.items.stream_all():
                streamed += 1
                if streamed == 1:
                    streaming.set()
                    await attempted.wait()  # the sibling's write runs while this stream is open
            return streamed

        async def write() -> str:
            await streaming.wait()
            try:
                await self.items.save(StItem(name="sibling"))
            finally:
                attempted.set()
            return "sibling"

        return await asyncio.gather(consume(), write(), return_exceptions=True)

    @transactional
    async def break_out_of_a_stream(self, then_fail: bool) -> int:
        await self.items.save(StItem(name="before-the-stream"))
        streamed = 0
        async for _item in self.items.stream_all():
            streamed += 1
            if streamed == 10:
                break  # the stream is left open: nothing closes it before the unit completes
        if then_fail:
            raise ValueError("the unit failed after abandoning its stream")
        return streamed

    @transactional(propagation=Propagation.NESTED)
    async def first_ten_in_a_nested_step(self, then_fail: bool) -> int:
        await self.items.save(StItem(name="in-the-step"))
        streamed = 0
        async for _item in self.items.stream_all():
            streamed += 1
            if streamed == 10:
                break  # the stream is left open as the NESTED step ends
        if then_fail:
            raise ValueError("the step failed after abandoning its stream")
        return streamed

    @transactional
    async def a_nested_step_abandons_its_stream(self, then_fail: bool) -> object:
        try:
            outcome: object = await self.first_ten_in_a_nested_step(then_fail)
        except ValueError as error:
            outcome = error
        await self.items.save(StItem(name="after-the-step"))
        return outcome

    @transactional
    async def a_savepoint_block_abandons_its_stream(self) -> int:
        streamed = await self.items.first_ten_in_a_savepoint_block()
        await self.items.save(StItem(name="after-the-block"))
        return streamed

    @transactional
    async def write_after_closing_a_stream_early(self) -> int:
        streamed = 0
        async with contextlib.aclosing(self.items.stream_all()) as stream:
            async for _item in stream:
                streamed += 1
                if streamed == 10:
                    break
        await self.items.save(StItem(name="after-close"))
        return streamed

    @transactional
    async def stream_slowly(self, fetched: asyncio.Event) -> int:
        return await self.items.stream_slowly(fetched)

    @transactional
    async def read_everything(self) -> int:
        return len([item async for item in self.items.stream_all()])

    @transactional
    async def write_after_exhausting_a_stream(self) -> int:
        streamed = 0
        async for _item in self.items.stream_all():
            streamed += 1
        await self.items.save(StItem(name="after-exhaust"))
        return streamed


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext, *, single_result: bool) -> None:
        self.backend = backend
        self.ctx = ctx
        self.single_result_connection = single_result

    @property
    def service(self) -> StService:
        return self.ctx.get_bean(StService)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def extra_rows(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT name FROM st_item WHERE name NOT LIKE 's-%' ORDER BY name"))
                return [row[0] for row in result.all()]
        finally:
            await engine.dispose()


@pytest.fixture
def single_result(relational_backend: RelationalBackend) -> bool:
    """Whether the unit's connection has one active result at a time (MySQL and MariaDB);
    ``test_unit_of_work_streams_single_result`` overrides it for SQLite with the capability turned off."""
    return relational_backend.lane in (MYSQL, MARIADB)


@pytest.fixture
async def harness(relational_backend: RelationalBackend, single_result: bool) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(StItem)
    ctx = ApplicationContext(relational_backend.config())
    for bean in (RelationalAutoConfiguration, StItemRepository, StService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        started = Harness(relational_backend, ctx, single_result=single_result)
        await started.service.seed()
        yield started
        # The connection went back to the pool healthy: the next unit works on it.
        assert started.checked_out() == 0
        assert await bounded(started.ctx.get_bean(StItemRepository).count()) >= ROWS
    finally:
        await ctx.stop()


async def bounded(call: Awaitable[T]) -> T:
    return await asyncio.wait_for(call, BOUND)


async def test_a_statement_between_two_fetches_of_the_same_task(harness: Harness) -> None:
    if not harness.single_result_connection:
        assert await bounded(harness.service.count_while_streaming()) == (ROWS, [ROWS, ROWS, ROWS])
        return
    with pytest.raises(IllegalTransactionStateError, match="streamed result .* is still open") as refused:
        await bounded(harness.service.count_while_streaming())
    assert "st_item" in str(refused.value)  # it names the stream
    assert await harness.extra_rows() == []


async def test_a_sibling_write_beside_an_open_stream(harness: Harness) -> None:
    streamed, written = await bounded(harness.service.write_beside_a_stream())
    if harness.single_result_connection:
        assert isinstance(written, IllegalTransactionStateError), repr(written)
        assert "streamed result" in str(written)
        assert streamed == ROWS  # the stream went on, and the unit committed without the refused write
        assert await harness.extra_rows() == []
    else:
        assert written == "sibling"
        assert streamed in (ROWS, ROWS + 1)
        assert await harness.extra_rows() == ["sibling"]


async def test_a_stream_abandoned_open_does_not_break_the_commit(harness: Harness) -> None:
    assert await bounded(harness.service.break_out_of_a_stream(then_fail=False)) == 10
    assert await harness.extra_rows() == ["before-the-stream"]


async def test_a_stream_abandoned_open_does_not_break_the_rollback(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="abandoning its stream"):
        await bounded(harness.service.break_out_of_a_stream(then_fail=True))
    assert await harness.extra_rows() == []
    assert not [record for record in caplog.records if record.getMessage() == "unit_of_work_rollback_failed"]


async def test_a_stream_closed_early_frees_the_connection(harness: Harness) -> None:
    assert await bounded(harness.service.write_after_closing_a_stream_early()) == 10
    assert await harness.extra_rows() == ["after-close"]


async def test_an_exhausted_stream_frees_the_connection(harness: Harness) -> None:
    assert await bounded(harness.service.write_after_exhausting_a_stream()) == ROWS
    assert await harness.extra_rows() == ["after-exhaust"]


async def test_a_stream_of_its_own_closed_early_frees_its_connection(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    items = harness.ctx.get_bean(StItemRepository)

    async def read_ten() -> int:  # outside a transaction: the stream opens a read unit of its own
        streamed = 0
        async with contextlib.aclosing(items.stream_all()) as stream:
            async for _item in stream:
                streamed += 1
                if streamed == 10:
                    break
        return streamed

    with caplog.at_level(logging.WARNING):
        assert await bounded(read_ten()) == 10
    assert harness.checked_out() == 0
    assert not [record for record in caplog.records if record.getMessage() == "unit_of_work_rollback_failed"]


@pytest.mark.parametrize("then_fail", [False, True], ids=["step-returns", "step-raises"])
async def test_a_nested_step_that_leaves_its_stream_open_still_ends_at_its_savepoint(
    harness: Harness, then_fail: bool
) -> None:
    outcome = await bounded(harness.service.a_nested_step_abandons_its_stream(then_fail))
    if then_fail:
        assert isinstance(outcome, ValueError), repr(outcome)
        assert await harness.extra_rows() == ["after-the-step"]  # rolled back to its savepoint, stream and all
    else:
        assert outcome == 10
        assert await harness.extra_rows() == ["after-the-step", "in-the-step"]


async def test_a_savepoint_block_that_leaves_its_stream_open_still_ends(harness: Harness) -> None:
    assert await bounded(harness.service.a_savepoint_block_abandons_its_stream()) == 10
    assert await harness.extra_rows() == ["after-the-block", "in-the-block"]


@pytest.mark.backends(MYSQL, MARIADB)
async def test_a_stream_cancelled_in_mid_fetch_ends_as_the_cancellation_at_once(harness: Harness) -> None:
    fetched = asyncio.Event()
    call = asyncio.create_task(harness.service.stream_slowly(fetched))
    await bounded(fetched.wait())
    call.cancel()  # lands in a fetch: the server spends 0.1 s on every row
    cancelled_at = time.perf_counter()
    done, _pending = await asyncio.wait({call}, timeout=BOUND)
    if not done:
        call.cancel()
        pytest.fail(f"the cancelled stream did not end within {BOUND} s")
    with pytest.raises(asyncio.CancelledError):
        call.result()
    # Its connection is in an unknown state: nothing more is read from it (not the ~30 s of rows left, nor
    # anything else), and the unit discards it.
    assert time.perf_counter() - cancelled_at < 5
    assert harness.checked_out() == 0


STEPS = 24


async def _cancelled_read(harness: Harness, kind: str, delay: float) -> str:
    if kind == "anyio":  # level-triggered, as under Starlette: every await of the task is cancelled again
        with anyio.move_on_after(delay) as scope:
            await harness.service.read_everything()
        return "cancelled" if scope.cancelled_caught else "completed"
    try:
        await asyncio.wait_for(harness.service.read_everything(), delay)
    except TimeoutError:
        return "cancelled"
    return "completed"


# A cancelled unit discards its connection with the unbuffered result still open on it; when the garbage
# collector later reaches that result, asyncmy's MySQLResult.__del__ calls _finish_unbuffered_query()
# without awaiting it. Nothing is sent (the coroutine never runs), and the connection is gone already.
@pytest.mark.filterwarnings("ignore:coroutine 'MySQLResult._finish_unbuffered_query' was never awaited")
@pytest.mark.parametrize("kind", ["anyio", "wait_for"])
async def test_a_cancel_anywhere_in_a_stream_ends_as_the_cancellation_and_leaves_nothing_behind(
    harness: Harness, kind: str
) -> None:
    started = time.perf_counter()
    assert await bounded(harness.service.read_everything()) == ROWS
    full = time.perf_counter() - started
    outcomes: Counter[str] = Counter()
    for step in range(STEPS):
        delay = full * 1.3 * step / STEPS
        where = f"{kind} step {step} ({delay * 1000:.3f} ms of a {full * 1000:.3f} ms unit)"
        call = asyncio.ensure_future(_cancelled_read(harness, kind, delay))
        done, _pending = await asyncio.wait({call}, timeout=BOUND)
        if not done:
            call.cancel()
            pytest.fail(f"{where}: the cancelled stream did not end within {BOUND} s")
        try:
            outcomes[call.result()] += 1
        except Exception as error:  # noqa: BLE001 — the assertion reports it
            pytest.fail(f"{where}: ended with {error!r} instead of completing or being cancelled")
        assert harness.checked_out() == 0, f"{where}: a pooled connection is still checked out"
    assert outcomes["cancelled"] > 0, outcomes
