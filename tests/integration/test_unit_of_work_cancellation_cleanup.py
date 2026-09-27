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
"""Data access in cancellation cleanup keeps its own outcome (WP01-12).

A driver error that stands in for a cancellation (aiosqlite's ``ValueError('Connection closed')``, asyncmy's
``InterfaceError('Cancelled during execution')``) ends the call as the cancellation. Only a cancel request
that arrives while the operation, or the unit, runs makes that call: cleanup code runs while its task is
still being cancelled (``Task.cancelling() > 0`` inside ``except CancelledError:``, a ``finally:`` block
and anyio's ``with CancelScope(shield=True):``), and the compensation, audit and lock-release work done
there must see its ordinary failures as themselves. Here:

- a duplicate saved in a native ``except CancelledError:`` handler raises ``DuplicateKeyException`` (the
  repository's translation of the ``IntegrityError``, raised from it);
- a ``@transactional`` call that raises a business exception in such a handler, or in anyio's shielded
  ``finally:`` cleanup, raises that exception, and the rest of the cleanup runs;
- a participant that fails in its own unit's cancellation handler raises its own exception there;
- an ``infrastructure_unit`` that hits a duplicate in shielded cleanup raises ``IntegrityError``;
- a unit whose body swallowed its own deadline's cancellation (``Task.uncancel()``) and then raised a
  business exception raises that exception, not ``TransactionTimedOutError``;
- a ``@transactional`` body whose own ``finally:`` cleanup (native, or anyio's shielded scope) saves a
  duplicate while its unit is being cancelled raises ``DuplicateKeyException``: the statement's own
  operation judged that failure (the translation is raised from it), so the unit never mistakes it for a
  driver's stand-in for the cancellation;
- a business exception such a body raises there ends the unit as cancelled, because the unit cannot tell
  it from a driver error raised outside a guarded statement, and it is logged at WARNING with its
  traceback instead of disappearing;
- a business exception raised there *from* a statement's failure (``raise DomainError() from error``)
  keeps its type, since the judgment follows ``__cause__``; raised while merely handling that failure
  (``except DataIntegrityException: raise DomainError()``) it ends the unit as cancelled and is logged at
  WARNING.

A test cancels a unit's body once the body says it has done its work and waits (``CcService.working``), never
after a fixed delay: on a loaded machine a cancel after a delay lands in the work itself, before the cleanup
under test is armed.

After each, no pooled connection is checked out, PostgreSQL has no backend idle in transaction, and the
next unit commits; a unit whose statement failed in cleanup returned its healthy connection to the pool
instead of discarding it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import anyio
import pytest
from sqlalchemy import Integer, String, event, insert, text
from sqlalchemy.exc import IntegrityError
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
from pyfly.data.transaction import infrastructure_unit
from pyfly.kernel.exceptions import DataIntegrityException, DuplicateKeyException
from tests.support.backend_matrix import MYSQL, PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG, MYSQL)


class InsufficientFundsError(Exception):
    """A business exception."""


class DuplicateItemError(Exception):
    """A business exception a duplicate key is translated into."""


class CcItem(Base):
    __tablename__ = "cc_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64))


@repository
class CcItemRepository(Repository[CcItem, int]):
    pass


@service
class CcLedger:
    def __init__(self, items: CcItemRepository) -> None:
        self.items = items

    @transactional
    async def debit(self) -> None:
        await self.items.save(CcItem(id=99, name="debit"))
        raise InsufficientFundsError("no money")


@service
class CcService:
    def __init__(self, items: CcItemRepository, ledger: CcLedger) -> None:
        self.items = items
        self.ledger = ledger
        self.seen: list[str] = []
        # Set when a body has done its work and waits to be cancelled: a test cancels then, not after a fixed
        # delay (under load, a cancel after a delay can land in the work, before the cleanup is armed).
        self.working = asyncio.Event()

    @transactional
    async def compensate_with_a_failing_participant(self) -> None:
        await self.items.save(CcItem(id=10, name="work"))
        self.working.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            try:
                await self.ledger.debit()  # joins this unit, in this unit's cancellation handler
            except InsufficientFundsError:
                self.seen.append("InsufficientFundsError")
            raise

    @transactional(timeout=0.1)
    async def swallow_the_deadline_then_fail(self) -> None:
        # The work comes after the deadline: done first, a slow save (a loaded machine) could take the deadline.
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            assert task is not None
            task.uncancel()  # the body handles its deadline itself
        await self.items.save(CcItem(id=20, name="late"))
        raise InsufficientFundsError("declined after the deadline")

    @transactional
    async def work_then_save_a_duplicate_in_its_own_cleanup(self) -> None:
        await self.items.save(CcItem(id=30, name="work"))
        self.working.set()
        try:
            await asyncio.sleep(10)
        finally:
            await self.items.save(CcItem(id=1, name="duplicate"))  # while this unit is being cancelled

    @transactional
    async def work_then_save_a_duplicate_in_its_own_shielded_cleanup(self) -> None:
        await self.items.save(CcItem(id=30, name="work"))
        self.working.set()
        try:
            await anyio.sleep(10)
        finally:
            with anyio.CancelScope(shield=True):
                await self.items.save(CcItem(id=1, name="duplicate"))

    @transactional
    async def work_then_translate_a_duplicate_in_its_own_cleanup(self, chained: bool) -> None:
        await self.items.save(CcItem(id=30, name="work"))
        self.working.set()
        try:
            await asyncio.sleep(10)
        finally:
            try:
                await self.items.save(CcItem(id=1, name="duplicate"))  # while this unit is being cancelled
            except DataIntegrityException as error:
                if chained:
                    raise DuplicateItemError("item 1 exists") from error
                raise DuplicateItemError("item 1 exists")  # noqa: B904 — the unchained form is the point

    @transactional
    async def work_then_fail_in_its_own_cleanup(self) -> None:
        await self.items.save(CcItem(id=30, name="work"))
        self.working.set()
        try:
            await asyncio.sleep(10)
        finally:
            raise InsufficientFundsError("refund declined while cancelling")


class Cleanup:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx
        self.connections_opened = 0
        engine = ctx.get_bean(DataSourceRegistry).primary.engine
        event.listen(engine.sync_engine, "connect", self._opened)

    def _opened(self, _connection: object, _record: object) -> None:
        self.connections_opened += 1

    @property
    def items(self) -> CcItemRepository:
        return self.ctx.get_bean(CcItemRepository)

    @property
    def ledger(self) -> CcLedger:
        return self.ctx.get_bean(CcLedger)

    @property
    def service(self) -> CcService:
        return self.ctx.get_bean(CcService)

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def _query(self, sql: str) -> list[object]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [row[0] for row in (await conn.execute(text(sql))).all()]
        finally:
            await engine.dispose()

    async def committed(self) -> list[object]:
        return await self._query("SELECT name FROM cc_item ORDER BY id")

    async def idle_in_transaction(self) -> int:
        if self.backend.dialect != "postgresql":
            return 0
        rows = await self._query(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
        )
        return int(str(rows[0]))

    async def seed(self) -> None:
        await self.items.save(CcItem(id=1, name="first"))


@pytest.fixture
async def cleanup(relational_backend: RelationalBackend) -> AsyncIterator[Cleanup]:
    await relational_backend.create_tables(CcItem)
    ctx = ApplicationContext(relational_backend.config())
    for bean in (RelationalAutoConfiguration, CcItemRepository, CcLedger, CcService):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        harness = Cleanup(relational_backend, ctx)
        await harness.seed()
        yield harness
        assert harness.checked_out() == 0
        assert await harness.idle_in_transaction() == 0
        await harness.items.save(CcItem(id=1000, name="next"))  # the next unit commits
        assert (await harness.committed())[-1] == "next"
    finally:
        await ctx.stop()


async def _cancel_soon(task: asyncio.Task[None]) -> None:
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _cancel_once_working(cleanup: Cleanup, task: asyncio.Task[None]) -> asyncio.Task[None]:
    """Cancel *task* once its body has done its work and waits (``CcService.working``), and return it."""
    await asyncio.wait_for(cleanup.service.working.wait(), 10)
    task.cancel()
    return task


async def test_a_duplicate_saved_in_an_except_cancelled_handler_raises_integrity_error(cleanup: Cleanup) -> None:
    seen: list[str] = []

    async def worker() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            try:
                await cleanup.items.save(CcItem(id=1, name="duplicate"))
            except DuplicateKeyException:
                seen.append("DuplicateKeyException")
            seen.append("rest of the cleanup ran")
            raise

    opened = cleanup.connections_opened
    await _cancel_soon(asyncio.create_task(worker()))
    assert seen == ["DuplicateKeyException", "rest of the cleanup ran"]
    assert cleanup.checked_out() == 0
    assert cleanup.connections_opened == opened  # the failed unit's connection was not discarded
    assert await cleanup.committed() == ["first"]


async def test_a_business_exception_in_an_except_cancelled_handler_keeps_its_type(cleanup: Cleanup) -> None:
    seen: list[str] = []

    async def worker() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            try:
                await cleanup.ledger.debit()
            except InsufficientFundsError:
                seen.append("InsufficientFundsError")
            seen.append("rest of the cleanup ran")
            raise

    await _cancel_soon(asyncio.create_task(worker()))
    assert seen == ["InsufficientFundsError", "rest of the cleanup ran"]
    assert await cleanup.committed() == ["first"]


async def test_a_business_exception_in_anyio_shielded_cleanup_keeps_its_type(cleanup: Cleanup) -> None:
    seen: list[str] = []
    with anyio.move_on_after(0.05) as scope:
        try:
            await anyio.sleep(10)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await cleanup.ledger.debit()
                except InsufficientFundsError:
                    seen.append("InsufficientFundsError")
                seen.append("rest of the cleanup ran")
    assert scope.cancelled_caught
    assert seen == ["InsufficientFundsError", "rest of the cleanup ran"]
    assert cleanup.checked_out() == 0
    assert await cleanup.committed() == ["first"]


async def test_a_participant_failing_in_its_units_cancellation_handler_keeps_its_type(cleanup: Cleanup) -> None:
    task = await _cancel_once_working(
        cleanup, asyncio.create_task(cleanup.service.compensate_with_a_failing_participant())
    )
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup.service.seen == ["InsufficientFundsError"]
    assert await cleanup.committed() == ["first"]  # the cancelled unit rolled back


async def test_an_infrastructure_unit_in_anyio_shielded_cleanup_raises_integrity_error(cleanup: Cleanup) -> None:
    seen: list[str] = []
    opened = cleanup.connections_opened
    with anyio.move_on_after(0.05) as scope:
        try:
            await anyio.sleep(10)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    async with infrastructure_unit(single_statement=True) as session:
                        await session.execute(insert(CcItem).values(id=1, name="duplicate"))
                except IntegrityError:
                    seen.append("IntegrityError")
                async with infrastructure_unit() as session:
                    await session.execute(insert(CcItem).values(id=2, name="audit"))
                seen.append("rest of the cleanup ran")
    assert scope.cancelled_caught
    assert seen == ["IntegrityError", "rest of the cleanup ran"]
    assert cleanup.checked_out() == 0
    assert cleanup.connections_opened == opened
    assert await cleanup.committed() == ["first", "audit"]


async def test_a_business_exception_after_the_body_handled_its_deadline_keeps_its_type(cleanup: Cleanup) -> None:
    with pytest.raises(InsufficientFundsError):
        await cleanup.service.swallow_the_deadline_then_fail()
    assert await cleanup.committed() == ["first"]


async def test_a_duplicate_saved_in_the_cancelled_units_own_cleanup_raises_integrity_error(cleanup: Cleanup) -> None:
    opened = cleanup.connections_opened
    task = await _cancel_once_working(
        cleanup, asyncio.create_task(cleanup.service.work_then_save_a_duplicate_in_its_own_cleanup())
    )
    with pytest.raises(DuplicateKeyException):
        await task
    assert cleanup.connections_opened == opened  # a failed statement leaves a healthy connection
    assert await cleanup.committed() == ["first"]


async def test_a_duplicate_saved_in_the_cancelled_units_own_shielded_cleanup_raises_integrity_error(
    cleanup: Cleanup,
) -> None:
    with pytest.raises(DuplicateKeyException), anyio.CancelScope() as scope:
        # anyio's level-triggered cancellation, delivered once the body has done its work.
        watcher = asyncio.ensure_future(cleanup.service.working.wait())
        watcher.add_done_callback(lambda _done: scope.cancel())
        await cleanup.service.work_then_save_a_duplicate_in_its_own_shielded_cleanup()
    assert await cleanup.committed() == ["first"]


async def test_a_business_exception_in_the_cancelled_units_own_cleanup_is_logged(
    cleanup: Cleanup, caplog: pytest.LogCaptureFixture
) -> None:
    task = await _cancel_once_working(cleanup, asyncio.create_task(cleanup.service.work_then_fail_in_its_own_cleanup()))
    with (
        caplog.at_level(logging.WARNING, logger="pyfly.data.transaction.template"),
        pytest.raises(asyncio.CancelledError) as raised,
    ):
        await task
    assert isinstance(raised.value.__cause__, InsufficientFundsError)
    replaced = [
        record for record in caplog.records if record.getMessage() == "transaction_error_replaced_by_cancellation"
    ]
    assert len(replaced) == 1
    assert replaced[0].levelno == logging.WARNING
    assert replaced[0].exc_info is not None and isinstance(replaced[0].exc_info[1], InsufficientFundsError)
    assert await cleanup.committed() == ["first"]


async def test_a_business_exception_raised_from_a_failed_statement_in_the_units_own_cleanup_keeps_its_type(
    cleanup: Cleanup, caplog: pytest.LogCaptureFixture
) -> None:
    task = await _cancel_once_working(
        cleanup, asyncio.create_task(cleanup.service.work_then_translate_a_duplicate_in_its_own_cleanup(chained=True))
    )
    with (
        caplog.at_level(logging.WARNING, logger="pyfly.data.transaction.template"),
        pytest.raises(DuplicateItemError) as raised,
    ):
        await task
    assert isinstance(raised.value.__cause__, DuplicateKeyException)
    assert isinstance(raised.value.__cause__.__cause__, IntegrityError)
    assert not [r for r in caplog.records if r.getMessage() == "transaction_error_replaced_by_cancellation"]
    assert await cleanup.committed() == ["first"]


async def test_a_business_exception_raised_while_handling_a_failed_statement_ends_as_the_cancellation(
    cleanup: Cleanup, caplog: pytest.LogCaptureFixture
) -> None:
    task = await _cancel_once_working(
        cleanup, asyncio.create_task(cleanup.service.work_then_translate_a_duplicate_in_its_own_cleanup(chained=False))
    )
    with (
        caplog.at_level(logging.WARNING, logger="pyfly.data.transaction.template"),
        pytest.raises(asyncio.CancelledError) as raised,
    ):
        await task
    assert isinstance(raised.value.__cause__, DuplicateItemError)
    assert isinstance(raised.value.__cause__.__context__, DuplicateKeyException)  # linked, not raised from it
    replaced = [r for r in caplog.records if r.getMessage() == "transaction_error_replaced_by_cancellation"]
    assert len(replaced) == 1 and replaced[0].levelno == logging.WARNING
    assert await cleanup.committed() == ["first"]
