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
"""Projections over ``SqlAlchemyEventStore`` on every relational lane (C066, C067, C068, C179).

The runner kept its cursor in memory, so every restart replayed the whole store into the read model; every
replica ran its own runner, so a shared read model got each event once per replica; and it slept a full poll
interval after every page, capping catch-up at 100 events a second. Its only test ran one runner, once, on the
in-memory store. These tests run a non-idempotent read model (a running total plus a ledger row per event, on
the checkpoint's datasource) through restarts, two replicas, a handler that fails between its two writes, a
late-committing writer, and a catch-up of a thousand events with the default settings.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, detached, infrastructure_unit
from pyfly.eventsourcing.checkpoint import SqlAlchemyCheckpointStore
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner
from pyfly.eventsourcing.store import SqlAlchemyEventStore
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from tests.integration.test_event_store_matrix import _append_after_another_unit_committed
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend

read_model = MetaData()
totals = Table(
    "wp08_totals",
    read_model,
    Column("name", String(32), primary_key=True),
    Column("total", Integer, nullable=False),
)
ledger = Table(
    "wp08_ledger",
    read_model,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False),
    Column("applied_by", String(32), nullable=False),
)
# The position strategy of the event stores the scenarios build; the xid8 reruns at the end set it for PostgreSQL.
_STRATEGY = "auto"


def _deposit(amount: int = 1) -> StoredEventEnvelope:
    return StoredEventEnvelope(event_type="Deposited", payload={"amount": amount})


class Setup:
    """An event store, a read model and checkpoints on one lane's database."""

    def __init__(self, backend: RelationalBackend) -> None:
        self.backend = backend
        self.engine: AsyncEngine = backend.create_engine()
        self.store = SqlAlchemyEventStore(self.engine, position_strategy=_STRATEGY)

    async def start(self) -> Setup:
        await self.store.start()
        await self.backend.create_tables(totals, ledger)
        async with self.engine.begin() as connection:
            await connection.execute(insert(totals).values(name="deposits", total=0))
        return self

    async def append(self, count: int, *, prefix: str = "acc") -> None:
        for index in range(count):
            await self.store.append(f"{prefix}-{index}", "Account", [_deposit()], expected_version=0)

    def checkpoints(self) -> SqlAlchemyCheckpointStore:
        return SqlAlchemyCheckpointStore(self.engine)

    def handler(self, applied_by: str) -> Callable[[StoredEventEnvelope], Awaitable[None]]:
        """The read model's handler: two writes per event, neither idempotent."""

        async def handle(event: StoredEventEnvelope) -> None:
            async with infrastructure_unit(self.engine) as session:
                await session.execute(insert(ledger).values(event_id=event.event_id, applied_by=applied_by))
                amount = int(event.payload["amount"])
                await session.execute(
                    update(totals).where(totals.c.name == "deposits").values(total=totals.c.total + amount)
                )

        return handle

    def runner(self, applied_by: str, **options: object) -> ProjectionRunner:
        return ProjectionRunner(
            FunctionProjection("deposits", self.handler(applied_by)),
            self.store,
            checkpoints=options.pop("checkpoints", self.checkpoints()),  # type: ignore[arg-type]
            poll_interval_s=options.pop("poll_interval_s", 0.05),  # type: ignore[arg-type]
            **options,  # type: ignore[arg-type]
        )

    async def read_model(self) -> tuple[int, int, int]:
        """(total, ledger rows, distinct events in the ledger)."""
        async with self.engine.connect() as connection:
            total = (await connection.execute(select(totals.c.total))).scalar_one()
            rows = (await connection.execute(select(func.count()).select_from(ledger))).scalar_one()
            distinct = (await connection.execute(select(func.count(func.distinct(ledger.c.event_id))))).scalar_one()
        return int(total), int(rows), int(distinct)

    async def applied_by(self) -> dict[str, int]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(select(ledger.c.applied_by, func.count()).group_by(ledger.c.applied_by))
            return {str(name): int(count) for name, count in rows.all()}

    async def caught_up(self, checkpoints: SqlAlchemyCheckpointStore, *, timeout: float = 20.0) -> None:
        last = await self.store.last_position()
        deadline = time.monotonic() + timeout
        while (await checkpoints.position("deposits") or 0) < last:
            assert time.monotonic() < deadline, "the projection did not catch up"
            await asyncio.sleep(0.02)


async def _setup(backend: RelationalBackend) -> Setup:
    return await Setup(backend).start()


# ---------------------------------------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------------------------------------


async def test_a_checkpoint_starts_where_asked_and_moves_only_from_the_expected_position(
    relational_backend: RelationalBackend,
) -> None:
    setup = await _setup(relational_backend)
    checkpoints = setup.checkpoints()
    await checkpoints.start()

    assert await checkpoints.load("orders") == 0
    assert await checkpoints.load("audit", initial=40) == 40
    assert await checkpoints.load("audit", initial=99) == 40  # the stored position wins

    async with checkpoints.batch("orders", expected=0, position=10) as claimed:
        assert claimed.claimed
    assert claimed.position == 10
    async with checkpoints.batch("orders", expected=0, position=20) as stale:
        assert not stale.claimed  # another runner moved it: this batch must not run
    assert await checkpoints.load("orders") == 10

    with pytest.raises(RuntimeError):
        async with checkpoints.batch("orders", expected=10, position=30) as failed:
            assert failed.claimed
            raise RuntimeError("a handler failed")
    assert failed.position == 10
    assert await checkpoints.load("orders") == 10

    await checkpoints.reset("orders")
    assert await checkpoints.load("orders") == 0
    await checkpoints.reset("orders", position=7)
    assert await checkpoints.load("orders") == 7


async def test_a_checkpoint_commits_with_the_read_model_writes_of_its_batch(
    relational_backend: RelationalBackend,
) -> None:
    setup = await _setup(relational_backend)
    checkpoints = setup.checkpoints()
    handle = setup.handler("inline")
    event = _deposit(5)

    with pytest.raises(RuntimeError):
        async with checkpoints.batch("deposits", expected=0, position=1):
            await handle(event)
            raise RuntimeError("the next event failed")
    assert await setup.read_model() == (0, 0, 0)
    assert await checkpoints.load("deposits") == 0

    async with checkpoints.batch("deposits", expected=0, position=1):
        await handle(event)
    assert await setup.read_model() == (5, 1, 1)
    assert await checkpoints.load("deposits") == 1


# ---------------------------------------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------------------------------------


async def test_a_restarted_runner_resumes_from_its_checkpoint_without_replaying(
    relational_backend: RelationalBackend,
) -> None:
    """C066: every restart re-applied every event (a balance of 300 events read 600 after one restart)."""
    setup = await _setup(relational_backend)
    await setup.append(120)

    first = setup.runner("boot-1")
    await first.start()
    await setup.caught_up(setup.checkpoints())
    await first.stop()
    assert await setup.read_model() == (120, 120, 120)

    await setup.append(30, prefix="later")
    second = setup.runner("boot-2")  # a new process: new runner, new checkpoint store
    await second.start()
    await setup.caught_up(setup.checkpoints())
    await second.stop()

    assert await setup.read_model() == (150, 150, 150)
    assert await setup.applied_by() == {"boot-1": 120, "boot-2": 30}


async def test_two_replicas_on_one_read_model_apply_each_event_once(relational_backend: RelationalBackend) -> None:
    """C067: each of N replicas applied every event (a balance of 500 events read 1500 with three)."""
    setup = await _setup(relational_backend)
    replicas = [setup.runner(name, lease=LeaseLock(setup.engine, owner=f"node-{name}")) for name in ("pod-a", "pod-b")]
    for replica in replicas:
        await replica.start()
    try:
        await asyncio.gather(setup.append(40, prefix="x"), setup.append(40, prefix="y"))
        await setup.caught_up(setup.checkpoints())
    finally:
        for replica in replicas:
            await replica.stop()

    assert await setup.read_model() == (80, 80, 80)
    assert sorted((await setup.applied_by()).values()) == [80]  # the lease holder applied all, the other waited


async def test_without_a_lease_the_checkpoint_still_fences_two_runners(relational_backend: RelationalBackend) -> None:
    """The checkpoint moves only from the position a batch started at, in the batch's own unit: a runner whose
    batch lost that race rolls it back, so a read model on the checkpoint's datasource gets each event once."""
    setup = await _setup(relational_backend)
    await setup.append(60)
    runners = [setup.runner(name, lease=False, batch_size=7) for name in ("r1", "r2")]
    for runner in runners:
        await runner.start()
    try:
        await setup.caught_up(setup.checkpoints())
    finally:
        for runner in runners:
            await runner.stop()

    assert await setup.read_model() == (60, 60, 60)


async def test_a_standby_replica_takes_over_from_the_checkpoint_when_the_lease_holder_stops(
    relational_backend: RelationalBackend,
) -> None:
    setup = await _setup(relational_backend)
    await setup.append(10)
    active = setup.runner("active", lease=LeaseLock(setup.engine, owner="node-active"), lease_ttl_s=30.0)
    standby = setup.runner("standby", lease=LeaseLock(setup.engine, owner="node-standby"), lease_ttl_s=30.0)
    await active.start()
    await setup.caught_up(setup.checkpoints())
    await standby.start()
    try:
        await asyncio.sleep(0.2)
        await active.stop()  # releases its lease
        await setup.append(5, prefix="after")
        await setup.caught_up(setup.checkpoints())
    finally:
        await standby.stop()

    assert await setup.read_model() == (15, 15, 15)
    assert await setup.applied_by() == {"active": 10, "standby": 5}


async def test_a_handler_failing_between_its_two_writes_leaves_neither_and_is_retried(
    relational_backend: RelationalBackend,
) -> None:
    setup = await _setup(relational_backend)
    await setup.append(6)
    poisoned = (await setup.store.stream_all(limit=6))[3].event_id
    failures = {"left": 2}
    write = setup.handler("runner")

    async def flaky(event: StoredEventEnvelope) -> None:
        if event.event_id == poisoned and failures["left"] > 0:
            async with infrastructure_unit(setup.engine) as session:
                await session.execute(insert(ledger).values(event_id=event.event_id, applied_by="half-applied"))
            failures["left"] -= 1
            raise RuntimeError("the second write failed")
        await write(event)

    checkpoints = setup.checkpoints()
    runner = ProjectionRunner(
        FunctionProjection("deposits", flaky), setup.store, checkpoints=checkpoints, poll_interval_s=0.05
    )
    await runner.start()
    try:
        await setup.caught_up(checkpoints)
    finally:
        await runner.stop()

    assert failures["left"] == 0
    assert await setup.read_model() == (6, 6, 6)
    assert await setup.applied_by() == {"runner": 6}  # the half-applied writes rolled back with their batch


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_a_late_committing_event_reaches_the_projection(relational_backend: RelationalBackend) -> None:
    """C011 end to end: a writer that stays open while later writers commit is not skipped by the runner."""
    setup = await _setup(relational_backend)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(setup.engine))
    appended, release = asyncio.Event(), asyncio.Event()

    async def late_writer() -> None:
        async with template.transaction():
            await setup.store.append("late", "Account", [_deposit(100)], expected_version=0)
            appended.set()
            await release.wait()

    checkpoints = setup.checkpoints()
    runner = setup.runner("runner")
    await runner.start()
    writer = detached(late_writer())
    try:
        await asyncio.wait_for(appended.wait(), 10)
        await setup.append(5)
        await setup.caught_up(checkpoints)
    finally:
        release.set()
        await writer
    try:
        await setup.caught_up(checkpoints)
    finally:
        await runner.stop()

    assert await setup.read_model() == (105, 6, 6)


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_a_projection_gets_an_aggregate_s_events_in_sequence_order_across_units(
    relational_backend: RelationalBackend,
) -> None:
    """Review of WP08: under ``xid8`` a deposit appended by a unit that took its transaction id before the
    account's opening committed streamed before the opening, and a projection that needs the account open
    retried the deposit for good (its checkpoint never passed it)."""
    setup = await _setup(relational_backend)
    await _append_after_another_unit_committed(setup.store)
    opened: set[str] = set()
    applied: list[tuple[str, str]] = []

    async def balances(event: StoredEventEnvelope) -> None:
        if event.event_type == "Opened":
            opened.add(event.aggregate_id)
        elif event.event_type == "Deposited" and event.aggregate_id not in opened:
            raise LookupError(f"a deposit to {event.aggregate_id}, which is not open")
        applied.append((event.aggregate_id, event.event_type))

    checkpoints = setup.checkpoints()
    runner = ProjectionRunner(
        FunctionProjection("deposits", balances), setup.store, checkpoints=checkpoints, poll_interval_s=0.05
    )
    await runner.start()
    try:
        await setup.caught_up(checkpoints, timeout=10.0)
    finally:
        await runner.stop()

    assert [kind for aggregate, kind in applied if aggregate == "account-x"] == ["Opened", "Deposited"]


async def test_catch_up_runs_at_full_speed_with_the_default_settings(relational_backend: RelationalBackend) -> None:
    """C068: the runner slept a poll interval after every page, 100 events a second at the defaults."""
    setup = await _setup(relational_backend)
    for block in range(10):
        await setup.store.append(f"bulk-{block}", "Account", [_deposit() for _ in range(100)], expected_version=0)
    seen: list[str] = []

    async def count(event: StoredEventEnvelope) -> None:
        seen.append(event.event_id)

    checkpoints = setup.checkpoints()
    runner = ProjectionRunner(FunctionProjection("deposits", count), setup.store, checkpoints=checkpoints)
    started = time.monotonic()
    await runner.start()
    try:
        await setup.caught_up(checkpoints)
    finally:
        await runner.stop()
    elapsed = time.monotonic() - started

    assert len(seen) == 1000 and len(set(seen)) == 1000
    # Sleeping a poll interval after each page, as before, takes at least 9 s; the margin absorbs a loaded host.
    assert elapsed < 5.0, f"1000 events took {elapsed:.2f}s ({1000 / elapsed:.0f} events/s)"


async def test_catch_up_with_pages_larger_than_a_numbering_round_never_sleeps_while_behind(
    relational_backend: RelationalBackend,
) -> None:
    """Review of WP08: a page read numbered one round (1000 events) of a backlog, so a page larger than that came
    back short and the runner slept a poll interval every 1000 events, however far behind it was."""
    setup = await _setup(relational_backend)
    assert setup.store.position_strategy == "head-row"
    for block in range(5):
        await setup.store.append(f"bulk-{block}", "Account", [_deposit() for _ in range(1000)], expected_version=0)
    seen: list[str] = []

    async def count(event: StoredEventEnvelope) -> None:
        seen.append(event.event_id)

    checkpoints = setup.checkpoints()
    runner = ProjectionRunner(
        FunctionProjection("deposits", count),
        setup.store,
        checkpoints=checkpoints,
        batch_size=2000,
        poll_interval_s=10.0,
    )
    started = time.monotonic()
    await runner.start()
    try:
        # Not setup.caught_up(): its last_position() would number the whole backlog before the runner reads it.
        while (await checkpoints.position("deposits") or 0) < 5000:
            assert time.monotonic() - started < 30.0, f"the projection did not catch up ({len(seen)} events)"
            await asyncio.sleep(0.02)
    finally:
        await runner.stop()
    elapsed = time.monotonic() - started

    assert len(seen) == 5000 and len(set(seen)) == 5000
    assert elapsed < 10.0, f"the runner slept a poll interval while behind: {elapsed:.2f}s for 5000 events"


async def test_a_rebuild_is_an_explicit_reset_and_a_new_projection_can_start_at_the_head(
    relational_backend: RelationalBackend,
) -> None:
    setup = await _setup(relational_backend)
    await setup.append(4)
    checkpoints = setup.checkpoints()

    tail = setup.runner("tail", start_from="latest")
    await tail.start()
    await setup.append(2, prefix="new")
    await setup.caught_up(checkpoints)
    await tail.stop()
    assert await setup.read_model() == (2, 2, 2)  # the history before its first start was left alone

    async with setup.engine.begin() as connection:
        await connection.execute(ledger.delete())
        await connection.execute(update(totals).values(total=0))
    await checkpoints.reset("deposits")
    rebuild = setup.runner("rebuild")
    await rebuild.start()
    await setup.caught_up(checkpoints)
    await rebuild.stop()
    assert await setup.read_model() == (6, 6, 6)


# ---------------------------------------------------------------------------------------------------------
# PostgreSQL's xid8 accelerator
# ---------------------------------------------------------------------------------------------------------

_XID8_SCENARIOS: tuple[Callable[[RelationalBackend], Awaitable[None]], ...] = (
    test_a_restarted_runner_resumes_from_its_checkpoint_without_replaying,
    test_two_replicas_on_one_read_model_apply_each_event_once,
    test_without_a_lease_the_checkpoint_still_fences_two_runners,
    test_a_standby_replica_takes_over_from_the_checkpoint_when_the_lease_holder_stops,
    test_a_handler_failing_between_its_two_writes_leaves_neither_and_is_retried,
    test_a_late_committing_event_reaches_the_projection,
    test_a_projection_gets_an_aggregate_s_events_in_sequence_order_across_units,
    test_catch_up_runs_at_full_speed_with_the_default_settings,
    test_a_rebuild_is_an_explicit_reset_and_a_new_projection_can_start_at_the_head,
)


@pytest.mark.backends(PG)
@pytest.mark.parametrize("scenario", _XID8_SCENARIOS, ids=lambda scenario: scenario.__name__.removeprefix("test_"))
async def test_the_runner_over_the_xid8_strategy_on_postgresql(
    relational_backend: RelationalBackend,
    scenario: Callable[[RelationalBackend], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scenarios above run every lane over the default strategy (head-row, PostgreSQL's too); PostgreSQL runs
    them again over the event store's opt-in ``xid8`` accelerator."""
    monkeypatch.setattr(sys.modules[__name__], "_STRATEGY", "xid8")
    await scenario(relational_backend)
