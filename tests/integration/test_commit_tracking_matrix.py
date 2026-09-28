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
"""``track_commits()`` tells whether a block committed anything (WP13, the base of C018, C019 and C081).

An orchestration engine runs step code it does not control, and must know whether a step that failed, timed
out or was cancelled committed work first: such a step is compensated and never retried blindly, and a step
that rolled back is neither. Here, on a SQLite file and on PostgreSQL, with real units of work:

- a ``@transactional`` call that commits counts as committed, and one that raises as rolled back;
- a repository write outside a transaction (its write auto unit) counts; a read, a read-only unit and a
  participant that joined an enclosing unit do not (the enclosing unit's boundary is what commits);
- a unit whose ``COMMIT`` was in flight when its task was cancelled counts as committed, although the call
  raised ``CancelledError`` (the commit is shielded and lands);
- units opened by tasks started inside the block count, and units of ``detached()`` work do not;
- nested blocks both see a unit of the inner one;
- a single-statement unit (``infrastructure_unit(single_statement=True)``) runs on an autocommit connection
  on PostgreSQL, where its statement commits as it runs: one that fails or is cancelled after its statement
  ran may have committed (it used to count as rolled back, so a step whose outbox append, cache write or
  state upsert had committed could be retried and write twice). One that fails before any statement ran
  rolled back, and on SQLite, where the unit is a transaction, a failure rolls it back.

On MongoDB (a replica set, and a standalone server), a repository write that runs without a transaction is
such a unit too: ``save``, ``delete`` and the bulk deletes (one command each), a ``@query`` pipeline that writes
(``$out``, ``$merge``), and every write on a standalone server, which has no transactions. One that fails after
its command ran (an after-insert event action, a write concern failure, an ordered ``save_all`` stopped at a
failing document) may have stored its documents, and reports ``UNKNOWN``: a saga step that failed so is
compensated, never retried into a second order. A write auto unit that runs a transaction (``save_all`` on a
replica set) rolls back as a whole.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from beanie import Document, Insert, after_event, before_event, init_beanie
from pymongo import IndexModel, WriteConcern
from pymongo.errors import WriteConcernError
from sqlalchemy import Integer, String, insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import transactional
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.query import query
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import CommitTracker, detached, infrastructure_unit, track_commits
from pyfly.kernel.exceptions import DuplicateKeyException
from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.exceptions import StepFailedError
from pyfly.transactional.core.model import ExecutionPattern, RetryPolicy
from pyfly.transactional.core.step_invoker import StepInvoker
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import SagaExecutionOrchestrator
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.step_invoker import StepInvoker as SagaStepInvoker
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend
from tests.support.commit_gate import SQLITE_OVERRIDES, CommitGate
from tests.support.mongo import BeanieDatabase, beanie_database

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class TrackedRow(Base):
    __tablename__ = "commit_tracking_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64))


@repository
class TrackedRows(Repository[TrackedRow, int]):
    pass


class RolledBackError(Exception):
    """The business failure a unit rolls back on."""


@service
class TrackedWriter:
    def __init__(self, rows: TrackedRows) -> None:
        self.rows = rows

    @transactional
    async def write(self, row_id: int) -> None:
        await self.rows.save(TrackedRow(id=row_id, name="written"))

    @transactional
    async def write_then_fail(self, row_id: int) -> None:
        await self.rows.save(TrackedRow(id=row_id, name="rolled back"))
        raise RolledBackError(str(row_id))

    @transactional(read_only=True)
    async def read(self) -> int:
        return len(await self.rows.find_all())

    @transactional
    async def write_twice_in_one_unit(self, first: int, second: int) -> None:
        await self.write(first)  # joins this unit
        await self.write(second)


class Harness:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def writer(self) -> TrackedWriter:
        return self.ctx.get_bean(TrackedWriter)

    @property
    def rows(self) -> TrackedRows:
        return self.ctx.get_bean(TrackedRows)

    def gate(self) -> CommitGate:
        engine = self.ctx.get_bean(DataSourceRegistry).primary.engine
        return CommitGate(self.backend, engine, TrackedRow.__tablename__)

    async def committed(self) -> list[int]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(text(f"SELECT id FROM {TrackedRow.__tablename__} ORDER BY id"))
                return [int(row[0]) for row in rows.all()]
        finally:
            await engine.dispose()


@pytest.fixture
async def harness(relational_backend: RelationalBackend) -> AsyncIterator[Harness]:
    await relational_backend.create_tables(TrackedRow)
    overrides = SQLITE_OVERRIDES if relational_backend.lane == SQLITE_FILE else {}
    ctx = ApplicationContext(relational_backend.config(overrides))
    for bean in (RelationalAutoConfiguration, TrackedRows, TrackedWriter):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        yield Harness(relational_backend, ctx)
    finally:
        await ctx.stop()


def _counts(tracker: CommitTracker) -> tuple[int, int, int]:
    return tracker.committed, tracker.rolled_back, tracker.unknown


async def test_a_committed_unit_counts_as_committed(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.writer.write(1)
    assert _counts(commits) == (1, 0, 0)
    assert commits.may_have_committed


async def test_a_unit_that_raises_counts_as_rolled_back(harness: Harness) -> None:
    with track_commits() as commits, pytest.raises(RolledBackError):
        await harness.writer.write_then_fail(1)
    assert _counts(commits) == (0, 1, 0)
    assert not commits.may_have_committed
    assert await harness.committed() == []


async def test_a_repository_write_outside_a_transaction_counts_and_a_read_does_not(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.rows.save(TrackedRow(id=1, name="auto unit"))
        await harness.rows.find_all()
        await harness.writer.read()
    assert _counts(commits) == (1, 0, 0)


async def test_a_participant_is_not_counted_apart_from_the_unit_it_joined(harness: Harness) -> None:
    with track_commits() as commits:
        await harness.writer.write_twice_in_one_unit(1, 2)
    assert _counts(commits) == (1, 0, 0)
    assert await harness.committed() == [1, 2]


async def test_a_commit_in_flight_when_the_task_is_cancelled_counts_as_committed(harness: Harness) -> None:
    trackers: list[CommitTracker] = []

    async def step() -> None:
        with track_commits() as commits:
            trackers.append(commits)
            await harness.writer.write(7)

    async with harness.gate() as gate:
        await gate.close()
        task = asyncio.create_task(step())
        await gate.wait_for_commit()
        task.cancel()
        await asyncio.sleep(0.05)  # the cancellation lands while the COMMIT waits behind the gate
        assert not task.done()  # the commit is shielded: the task waits for it
        await gate.open()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _counts(trackers[0]) == (1, 0, 0)
    assert await harness.committed() == [7]


async def test_child_tasks_count_and_detached_work_does_not(harness: Harness) -> None:
    detached_done = asyncio.Event()

    async def detached_write() -> None:
        await harness.writer.write(3)
        detached_done.set()

    with track_commits() as commits:
        await asyncio.gather(harness.writer.write(1), harness.writer.write(2))
        await detached(detached_write())
    await asyncio.wait_for(detached_done.wait(), 5)
    assert _counts(commits) == (2, 0, 0)
    assert await harness.committed() == [1, 2, 3]


async def test_nested_blocks_both_see_a_unit_of_the_inner_one(harness: Harness) -> None:
    with track_commits() as outer:
        await harness.writer.write(1)
        with track_commits() as inner:
            await harness.writer.write(2)
    assert _counts(outer) == (2, 0, 0)
    assert _counts(inner) == (1, 0, 0)


async def test_a_unit_outside_every_block_reports_to_no_tracker(harness: Harness) -> None:
    with track_commits() as commits:
        pass
    await harness.writer.write(1)
    assert _counts(commits) == (0, 0, 0)


def _single_statement_autocommits(harness: Harness) -> bool:
    """Whether a single-statement unit runs on an autocommit connection on this lane (PostgreSQL)."""
    return harness.backend.lane == PG


async def test_a_single_statement_unit_that_fails_after_its_statement_ran_may_have_committed(
    harness: Harness,
) -> None:
    with track_commits() as commits, pytest.raises(RolledBackError):
        async with infrastructure_unit(None, single_statement=True) as session:
            await session.execute(insert(TrackedRow.__table__).values(id=1, name="single statement"))
            raise RolledBackError("the adapter fails after its statement")
    if _single_statement_autocommits(harness):
        assert _counts(commits) == (0, 0, 1)  # the statement committed as it ran
        assert commits.may_have_committed
        assert await harness.committed() == [1]
    else:
        assert _counts(commits) == (0, 1, 0)
        assert await harness.committed() == []


async def test_a_single_statement_unit_cancelled_after_its_statement_ran_may_have_committed(
    harness: Harness,
) -> None:
    with track_commits() as commits, pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            async with infrastructure_unit(None, single_statement=True) as session:
                await session.execute(insert(TrackedRow.__table__).values(id=2, name="single statement"))
                await asyncio.sleep(5)  # the timeout lands after the statement ran
    if _single_statement_autocommits(harness):
        assert _counts(commits) == (0, 0, 1)
        assert await harness.committed() == [2]
    else:
        assert _counts(commits) == (0, 1, 0)
        assert await harness.committed() == []


async def test_a_single_statement_unit_that_fails_before_any_statement_rolled_back(harness: Harness) -> None:
    with track_commits() as commits, pytest.raises(RolledBackError):
        async with infrastructure_unit(None, single_statement=True):
            raise RolledBackError("before any statement")
    assert _counts(commits) == (0, 1, 0)
    assert not commits.may_have_committed


async def test_a_single_statement_unit_that_returns_counts_as_committed(harness: Harness) -> None:
    with track_commits() as commits:
        async with infrastructure_unit(None, single_statement=True) as session:
            await session.execute(insert(TrackedRow.__table__).values(id=3, name="single statement"))
    assert _counts(commits) == (1, 0, 0)
    assert await harness.committed() == [3]


# -- MongoDB ---------------------------------------------------------------------------------------------------


class AuditFailedError(Exception):
    """An after-insert action's failure: the document was stored before it ran."""


class TrackedOrder(Document):
    ref: str
    status: str = "PLACED"
    failing_audits: ClassVar[list[str]] = []
    """The refs whose after-insert audit fails, once each."""
    refused: ClassVar[set[str]] = set()
    """The refs whose before-insert check refuses them (nothing is sent)."""

    class Settings:
        name = "commit_tracking_order"
        indexes = [IndexModel("ref", unique=True)]

    @before_event(Insert)
    def check(self) -> None:
        if self.ref in TrackedOrder.refused:
            raise RolledBackError(self.ref)

    @after_event(Insert)
    def audit(self) -> None:
        if self.ref in TrackedOrder.failing_audits:
            TrackedOrder.failing_audits.remove(self.ref)
            raise AuditFailedError(self.ref)


class TrackedOrders(MongoRepository[TrackedOrder, str]):
    async def place_then_fail(self, ref: str) -> None:
        """A method of the application's that sends a command of its own with the call's session, then fails."""
        await TrackedOrder(ref=ref).insert(session=self._session)
        raise RolledBackError(ref)


class UnacknowledgedOrder(Document):
    """An order on a client whose write concern (``w: 2``) the one-member replica set cannot satisfy: each write is
    applied, and only its acknowledgment fails."""

    ref: str

    class Settings:
        name = "commit_tracking_unacknowledged"


class UnacknowledgedOrders(MongoRepository[UnacknowledgedOrder, str]):
    @query('[{"$match": {}}, {"$out": "commit_tracking_unacknowledged_copy"}]')
    async def copy_all(self) -> list[dict[str, Any]]: ...


@pytest.fixture(params=["replica-set", "standalone"])
async def orders(request: pytest.FixtureRequest, mongo_rs_url: str, mongo_url: str) -> AsyncIterator[BeanieDatabase]:
    """The orders on a database of their own, on the replica set and on a standalone server."""
    TrackedOrder.failing_audits.clear()
    TrackedOrder.refused.clear()
    async with beanie_database(mongo_rs_url if request.param == "replica-set" else mongo_url, [TrackedOrder]) as db:
        yield db


@pytest.fixture
async def replica_set_orders(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    TrackedOrder.failing_audits.clear()
    TrackedOrder.refused.clear()
    async with beanie_database(mongo_rs_url, [TrackedOrder]) as db:
        yield db


@pytest.fixture
async def standalone_orders(mongo_url: str) -> AsyncIterator[BeanieDatabase]:
    TrackedOrder.failing_audits.clear()
    TrackedOrder.refused.clear()
    async with beanie_database(mongo_url, [TrackedOrder]) as db:
        yield db


@pytest.fixture
async def unacknowledged(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    """A database of its own, on which :class:`UnacknowledgedOrder` writes with ``w: 2`` (the test reads and seeds
    it with the client's default write concern)."""
    async with beanie_database(mongo_rs_url, []) as db:
        await init_beanie(
            database=db.client.get_database(db.name, write_concern=WriteConcern(w=2, wtimeout=500)),
            document_models=[UnacknowledgedOrder],
        )
        yield db


async def _stored(db: BeanieDatabase, collection: str = TrackedOrder.Settings.name) -> list[str]:
    """The refs stored in *collection*, read outside every unit."""
    return sorted(row["ref"] for row in await db.database[collection].find({}).to_list())


async def test_a_mongo_save_whose_after_insert_action_fails_may_have_committed(orders: BeanieDatabase) -> None:
    """``save`` is one command, and runs without a transaction (on a standalone server every write does): the
    document is stored before its after-insert action fails, and keeps the id it was stored with."""
    TrackedOrder.failing_audits.append("o-1")
    order = TrackedOrder(ref="o-1")
    with track_commits() as commits, pytest.raises(AuditFailedError):
        await TrackedOrders().save(order)
    assert _counts(commits) == (0, 0, 1)
    assert commits.may_have_committed
    assert await _stored(orders) == ["o-1"]
    assert [row["_id"] for row in await orders.database[TrackedOrder.Settings.name].find({}).to_list()] == [order.id]


async def test_a_mongo_save_that_fails_before_its_command_rolled_back(orders: BeanieDatabase) -> None:
    TrackedOrder.refused.add("o-2")
    with track_commits() as commits, pytest.raises(RolledBackError):
        await TrackedOrders().save(TrackedOrder(ref="o-2"))
    assert _counts(commits) == (0, 1, 0)
    assert not commits.may_have_committed
    assert await _stored(orders) == []


async def test_a_mongo_save_the_server_rejects_counts_as_unknown(orders: BeanieDatabase) -> None:
    """Conservative, as a statement on an autocommit connection: the unit does not tell a command the server
    rejected from one whose reply was lost after it applied."""
    await TrackedOrders().save(TrackedOrder(ref="o-3"))
    with track_commits() as commits, pytest.raises(DuplicateKeyException):
        await TrackedOrders().save(TrackedOrder(ref="o-3"))
    assert _counts(commits) == (0, 0, 1)
    assert await _stored(orders) == ["o-3"]


async def test_mongo_writes_that_return_count_as_committed(orders: BeanieDatabase) -> None:
    repository = TrackedOrders()
    with track_commits() as commits:
        await repository.save(TrackedOrder(ref="o-4"))
        await repository.save_all([TrackedOrder(ref="o-5"), TrackedOrder(ref="o-6")])
        await repository.find_all()
    assert _counts(commits) == (2, 0, 0)
    assert await _stored(orders) == ["o-4", "o-5", "o-6"]


async def test_a_mongo_save_all_that_fails_in_its_transaction_rolled_back(replica_set_orders: BeanieDatabase) -> None:
    """On a replica set ``save_all`` runs in a transaction: the after-insert action that fails aborts it, and
    nothing is stored."""
    TrackedOrder.failing_audits.append("o-8")
    with track_commits() as commits, pytest.raises(AuditFailedError):
        await TrackedOrders().save_all([TrackedOrder(ref="o-7"), TrackedOrder(ref="o-8")])
    assert _counts(commits) == (0, 1, 0)
    assert await _stored(replica_set_orders) == []


async def test_an_ordered_save_all_stopped_at_a_failing_document_on_a_standalone_server_may_have_committed(
    standalone_orders: BeanieDatabase,
) -> None:
    """Without transactions the bulk write stores the documents before the one that fails."""
    repository = TrackedOrders()
    await repository.save(TrackedOrder(ref="taken"))
    with track_commits() as commits, pytest.raises(DuplicateKeyException):
        await repository.save_all([TrackedOrder(ref="o-9"), TrackedOrder(ref="taken"), TrackedOrder(ref="o-10")])
    assert _counts(commits) == (0, 0, 1)
    assert await _stored(standalone_orders) == ["o-9", "taken"]


async def test_a_custom_write_on_a_standalone_server_that_fails_after_its_command_may_have_committed(
    standalone_orders: BeanieDatabase,
) -> None:
    """A repository method of the application's that calls Beanie with the call's session: its commands are not the
    framework's, and the unit counts the session it handed out as an operation begun."""
    with track_commits() as commits, pytest.raises(RolledBackError):
        await TrackedOrders().place_then_fail("o-13")
    assert _counts(commits) == (0, 0, 1)
    assert await _stored(standalone_orders) == ["o-13"]


async def test_a_mongo_save_whose_write_concern_fails_may_have_committed(unacknowledged: BeanieDatabase) -> None:
    order = UnacknowledgedOrder(ref="o-11")
    with track_commits() as commits, pytest.raises(WriteConcernError):
        await UnacknowledgedOrders().save(order)
    assert _counts(commits) == (0, 0, 1)
    assert await _stored(unacknowledged, UnacknowledgedOrder.Settings.name) == ["o-11"]


async def test_a_mongo_pipeline_that_writes_and_then_fails_may_have_committed(unacknowledged: BeanieDatabase) -> None:
    """A ``$out`` pipeline is one command, run without a transaction: here it replaces its collection, and only its
    write concern fails."""
    await unacknowledged.database[UnacknowledgedOrder.Settings.name].insert_one({"ref": "o-12"})
    repository = UnacknowledgedOrders()
    MongoRepositoryBeanPostProcessor().after_init(repository, "unacknowledgedOrders")
    with track_commits() as commits, pytest.raises(WriteConcernError):
        await repository.copy_all()
    assert _counts(commits) == (0, 0, 1)
    assert await _stored(unacknowledged, "commit_tracking_unacknowledged_copy") == ["o-12"]


class CheckoutOrder(Document):
    ref: str
    status: str = "PLACED"
    audit_failures: ClassVar[int] = 0
    """How many more inserts the audit hook fails (after the insert)."""

    class Settings:
        name = "commit_tracking_checkout_order"

    @after_event(Insert)
    def audit(self) -> None:
        if CheckoutOrder.audit_failures:
            CheckoutOrder.audit_failures -= 1
            raise AuditFailedError(self.ref)


class CheckoutOrders(MongoRepository[CheckoutOrder, str]):
    pass


@saga(name="commit-tracking-checkout")
class Checkout:
    """A saga whose step places an order (retried up to 3 attempts) and whose compensation cancels it."""

    def __init__(self) -> None:
        self.orders = CheckoutOrders()
        self.attempts = 0

    @saga_step(id="place", compensate="cancel", retry=3)
    async def place(self) -> str:
        self.attempts += 1
        order = await self.orders.save(CheckoutOrder(ref="order-2"))
        return str(order.id)

    async def cancel(self) -> None:
        for order in await self.orders.find_all(ref="order-2"):
            order.status = "CANCELLED"
            await self.orders.save(order)


@pytest.fixture(params=["replica-set", "standalone"])
async def checkout_orders(
    request: pytest.FixtureRequest, mongo_rs_url: str, mongo_url: str
) -> AsyncIterator[BeanieDatabase]:
    """The checkout orders on a database of their own; the audit hook fails once."""
    CheckoutOrder.audit_failures = 1
    async with beanie_database(mongo_rs_url if request.param == "replica-set" else mongo_url, [CheckoutOrder]) as db:
        yield db


async def _checkout_orders(db: BeanieDatabase) -> list[tuple[str, str]]:
    rows = await db.database[CheckoutOrder.Settings.name].find({}).to_list()
    return sorted((row["ref"], row["status"]) for row in rows)


async def test_a_step_whose_mongo_save_was_stored_before_it_failed_is_not_retried(
    checkout_orders: BeanieDatabase,
) -> None:
    """The step's order was inserted, then its audit hook failed: the step is compensated like a completed one, and
    not retried into a second order."""
    orders = CheckoutOrders()
    attempts = 0

    async def place_order() -> str:
        nonlocal attempts
        attempts += 1
        order = await orders.save(CheckoutOrder(ref="order-1"))
        return str(order.id)

    ctx = ExecutionContext(name="checkout", pattern=ExecutionPattern.SAGA, input={})
    with pytest.raises(StepFailedError) as failure:
        await StepInvoker().invoke(
            bean=None, method=place_order, step_id="place", ctx=ctx, retry_policy=RetryPolicy(max_attempts=3)
        )
    assert isinstance(failure.value.__cause__, AuditFailedError)
    assert attempts == 1
    assert ctx.has_step_committed("place")
    assert await _checkout_orders(checkout_orders) == [("order-1", "PLACED")]


async def test_a_saga_compensates_the_order_a_failed_mongo_step_stored_instead_of_placing_another(
    checkout_orders: BeanieDatabase,
) -> None:
    registry = SagaRegistry()
    checkout = Checkout()
    registry.register_from_bean(checkout)
    invoker = SagaStepInvoker(ArgumentResolver())
    engine = SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=SagaExecutionOrchestrator(invoker),
        compensator=SagaCompensator(invoker),
    )

    result = await engine.execute("commit-tracking-checkout")

    assert result.success is False
    assert isinstance(result.error, AuditFailedError)
    assert checkout.attempts == 1
    assert result.steps["place"].compensated is True
    assert await _checkout_orders(checkout_orders) == [("order-2", "CANCELLED")]
