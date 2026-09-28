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
"""``SqlAlchemyEventStore`` on every relational lane (C011, C070, F8's event-store half, C093).

The global stream paged by ``occurred_at``, a clock value stamped when the envelope was built: an event built
first and committed second was skipped for good, events with one timestamp were delivered again at every poll
(and more than a page of them stalled the stream), and each poll sorted the whole table. The store now gives
every event a global position no reader can see an event committed below later (by default a head-row counter
that numbers the committed events when the stream is read; on PostgreSQL, opt-in, the writer's ``xid8`` below
the readers' snapshot horizon), pages by it, and joins the ambient unit of work, so an aggregate's events commit
or roll back with the rest of the business transaction.

The sqlite-file lane runs in the fast suite; PostgreSQL, MySQL and MariaDB run with ``-m integration``, and
PostgreSQL runs the scenarios a second time with the ``xid8`` strategy (at the end of the module).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Column, MetaData, String, Table, Update, func, insert, select, update

from pyfly.data.relational.framework_schema import FrameworkSchemaError, event_store, event_store_head
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    Propagation,
    TransactionSynchronizationAdapter,
    TransactionTemplate,
    detached,
    infrastructure_unit,
    register_synchronization,
    resolve_manager,
)
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.store import ConcurrencyError, SqlAlchemyEventStore
from pyfly.kernel.exceptions import OptimisticLockingFailureException
from pyfly.testing import StatementCounter
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend

orders = Table(
    "wp08_orders",
    MetaData(),
    Column("id", String(64), primary_key=True),
    Column("name", String(64), nullable=False),
)


def _envelope(event_type: str, *, occurred_at: datetime | None = None, **payload: object) -> StoredEventEnvelope:
    envelope = StoredEventEnvelope(event_type=event_type, payload=dict(payload))
    if occurred_at is not None:
        envelope.occurred_at = occurred_at
    return envelope


# The position strategy of the stores the scenarios build; the xid8 reruns at the end set it for PostgreSQL.
_STRATEGY = "auto"


async def _store(backend: RelationalBackend, strategy: str | None = None, **options: object) -> SqlAlchemyEventStore:
    engine = backend.create_engine()
    store = SqlAlchemyEventStore(engine, position_strategy=strategy or _STRATEGY, **options)  # type: ignore[arg-type]
    await store.start()
    return store


async def _drain(store: SqlAlchemyEventStore, after: int = 0, *, limit: int = 100) -> list[StoredEventEnvelope]:
    """Every event after position *after*, page by page, as a projection reads them."""
    seen: list[StoredEventEnvelope] = []
    while True:
        page = await store.stream_all(after_position=after, limit=limit)
        if not page:
            return seen
        seen.extend(page)
        position = page[-1].global_position
        assert position is not None
        after = position


def _types(events: list[StoredEventEnvelope]) -> list[str]:
    return [event.event_type for event in events]


def _template(store: SqlAlchemyEventStore) -> TransactionTemplate:
    return TransactionTemplate(SqlAlchemyTransactionManager.for_engine(store.engine))


# ---------------------------------------------------------------------------------------------------------
# Appending and reading
# ---------------------------------------------------------------------------------------------------------


async def test_events_round_trip_with_their_sequence_metadata_and_global_position(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    placed = _envelope("OrderPlaced", amount=100)
    placed.metadata = {"correlation_id": "c-1"}
    placed.tenant_id = "tenant-a"
    await store.append("order-1", "Order", [placed, _envelope("OrderShipped", carrier="ups")], expected_version=0)
    await store.append("order-2", "Order", [_envelope("OrderPlaced", amount=7)], expected_version=0)

    loaded = await store.load("order-1")
    assert [(event.event_type, event.sequence, event.aggregate_id) for event in loaded] == [
        ("OrderPlaced", 1, "order-1"),
        ("OrderShipped", 2, "order-1"),
    ]
    assert loaded[0].payload == {"amount": 100}
    assert loaded[0].metadata == {"correlation_id": "c-1"}
    assert loaded[0].tenant_id == "tenant-a"
    assert loaded[0].occurred_at == placed.occurred_at
    assert await store.load("order-1", after_sequence=1) == loaded[1:]
    assert await store.latest_version("order-1") == 2
    assert await store.latest_version("nobody") == 0

    streamed = await store.stream_all()
    assert [(event.aggregate_id, event.sequence) for event in streamed] == [
        ("order-1", 1),
        ("order-1", 2),
        ("order-2", 1),
    ]
    positions = [event.global_position for event in streamed]
    assert all(position is not None for position in positions)
    assert positions == sorted(positions) and len(set(positions)) == 3
    assert [event.global_position for event in await store.load("order-1")] == positions[:2]
    assert await store.last_position() == positions[-1]
    async with store.engine.connect() as connection:
        stored_metadata = (
            await connection.execute(select(event_store.c.metadata).where(event_store.c.event_id == placed.event_id))
        ).scalar_one()
    assert json.loads(stored_metadata) == {"correlation_id": "c-1"}


async def test_an_envelope_equals_itself_read_back_whatever_its_global_position(
    relational_backend: RelationalBackend,
) -> None:
    """The global position is where the store placed the event, not part of the event: an envelope handed to
    ``append`` (no position yet) equals the same event loaded back (with one)."""
    store = await _store(relational_backend)
    appended = _envelope("Opened", amount=1)
    await store.append("acc", "Account", [appended], expected_version=0)
    await _drain(store)

    loaded = await store.load("acc")
    assert appended.global_position is None and loaded[0].global_position is not None
    assert loaded == [appended]


async def test_the_global_stream_follows_commit_order_not_the_envelope_clock(
    relational_backend: RelationalBackend,
) -> None:
    """C011/C070: the envelope built first and committed second was never delivered."""
    store = await _store(relational_backend)
    built_first = _envelope("BuiltFirstCommittedSecond")
    built_second = _envelope("BuiltSecondCommittedFirst")
    assert built_first.occurred_at < built_second.occurred_at

    await store.append("b", "Order", [built_second], expected_version=0)
    first_poll = await _drain(store)
    assert _types(first_poll) == ["BuiltSecondCommittedFirst"]
    cursor = first_poll[-1].global_position
    assert cursor is not None

    await store.append("a", "Order", [built_first], expected_version=0)
    assert _types(await _drain(store, cursor)) == ["BuiltFirstCommittedSecond"]


async def test_events_that_share_a_timestamp_are_each_streamed_once(relational_backend: RelationalBackend) -> None:
    """C011/C070: tied timestamps ping-ponged between pages, and more than a page of them (a second-precision
    column on MySQL) stalled the stream for good."""
    store = await _store(relational_backend)
    tied = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    for batch in range(3):
        await store.append(
            f"burst-{batch}",
            "Order",
            [_envelope(f"Burst{batch}-{index}", occurred_at=tied) for index in range(35)],
            expected_version=0,
        )
    await store.append(
        "later", "Order", [_envelope("Later", occurred_at=tied + timedelta(seconds=1))], expected_version=0
    )

    seen = await _drain(store, limit=10)
    assert len(seen) == 106
    assert len({event.event_id for event in seen}) == 106
    assert seen[-1].event_type == "Later"


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_an_append_waits_for_no_other_unit_and_a_late_commit_is_not_skipped_on_the_stream(
    relational_backend: RelationalBackend,
) -> None:
    """C011/C070 with a real open transaction (SQLite has one writer at a time, so the sequential test above
    covers it): the writer that built its envelope first commits after a later writer, and a reader polling
    in between never skips it."""
    await _late_commit_scenario(await _store(relational_backend))


async def _late_commit_scenario(store: SqlAlchemyEventStore) -> None:
    template = _template(store)
    appended = asyncio.Event()
    release = asyncio.Event()

    async def late_writer() -> None:
        async with template.transaction():
            await store.append("late", "Order", [_envelope("BuiltFirstCommittedSecond")], expected_version=0)
            appended.set()
            await release.wait()

    writer = detached(late_writer())
    try:
        await asyncio.wait_for(appended.wait(), 10)
        # The later writer is not held up by the open unit, and commits first.
        await asyncio.wait_for(
            store.append("early", "Order", [_envelope("BuiltSecondCommittedFirst")], expected_version=0), 10
        )
        seen = await _drain(store)
        cursor = seen[-1].global_position if seen else 0
        assert cursor is not None
    finally:
        release.set()
        await writer
    seen += await _drain(store, cursor)
    assert sorted(_types(seen)) == ["BuiltFirstCommittedSecond", "BuiltSecondCommittedFirst"]
    positions = [event.global_position for event in seen]
    assert positions == sorted(positions)  # type: ignore[type-var]


async def test_concurrent_appends_get_distinct_increasing_positions(relational_backend: RelationalBackend) -> None:
    store = await _store(relational_backend)
    await asyncio.gather(
        *(
            store.append(
                f"agg-{index}", "Order", [_envelope(f"E{index}-a"), _envelope(f"E{index}-b")], expected_version=0
            )
            for index in range(20)
        )
    )
    seen = await _drain(store, limit=7)
    assert len(seen) == 40 and len({event.event_id for event in seen}) == 40
    positions = [event.global_position for event in seen]
    assert len(set(positions)) == 40 and positions == sorted(positions)  # type: ignore[type-var]
    for index in range(20):
        mine = [event.event_type for event in seen if event.aggregate_id == f"agg-{index}"]
        assert mine == [f"E{index}-a", f"E{index}-b"]  # an aggregate's events keep their order


async def test_concurrent_appends_to_one_aggregate_let_one_win(relational_backend: RelationalBackend) -> None:
    store = await _store(relational_backend)
    await store.append("acc-1", "Account", [_envelope("Opened")], expected_version=0)

    results = await asyncio.gather(
        store.append("acc-1", "Account", [_envelope("Deposited")], expected_version=1),
        store.append("acc-1", "Account", [_envelope("Withdrawn")], expected_version=1),
        return_exceptions=True,
    )

    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1, results
    assert isinstance(errors[0], ConcurrencyError)
    assert isinstance(errors[0], OptimisticLockingFailureException)
    assert await store.latest_version("acc-1") == 2
    with pytest.raises(ConcurrencyError, match="expected version 5"):
        await store.append("acc-1", "Account", [_envelope("Closed")], expected_version=5)


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_an_aggregate_s_events_stream_in_sequence_order_across_units(
    relational_backend: RelationalBackend,
) -> None:
    """A unit that writes first, then appends to an aggregate after another unit appended to it and committed:
    under ``xid8`` its event got its transaction's (lower) id as its position and streamed before the event it
    follows, and a projection that needs the account opened before a deposit stalled on it for good. The guard
    refuses such an append (a ``ConcurrencyError``: the command runs again in a new unit); the head row numbers
    the events when they have committed."""
    store = await _store(relational_backend)
    attempts = await _append_after_another_unit_committed(store)

    events = await _drain(store)
    in_account = [(event.event_type, event.sequence) for event in events if event.aggregate_id == "account-x"]
    assert in_account == [("Opened", 1), ("Deposited", 2)]
    assert _types(events).count("OrderPlaced") == 1
    if store.position_strategy == "xid8":
        assert attempts == 2  # the guard refused the first attempt: its transaction id is below Opened's


async def _append_after_another_unit_committed(store: SqlAlchemyEventStore) -> int:
    """The command's unit writes first; another unit opens account-x and commits; the command then reads the
    account's version and deposits. A ``ConcurrencyError`` runs the command again in a new unit, as a command
    handler does. Returns the command's attempts."""
    template = _template(store)
    wrote, opened = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def command() -> None:
        nonlocal attempts
        while True:
            attempts += 1
            try:
                async with template.transaction():
                    await store.append("order-y", "Order", [_envelope("OrderPlaced")], expected_version=0)
                    wrote.set()
                    await opened.wait()
                    version = await store.latest_version("account-x")
                    await store.append("account-x", "Account", [_envelope("Deposited")], expected_version=version)
                return
            except ConcurrencyError:
                if attempts == 3:
                    raise

    unit = detached(command())
    await asyncio.wait_for(wrote.wait(), 10)
    await store.append("account-x", "Account", [_envelope("Opened")], expected_version=0)
    opened.set()
    await asyncio.wait_for(unit, 20)
    return attempts


async def test_after_event_id_still_pages_and_an_unknown_id_is_refused(relational_backend: RelationalBackend) -> None:
    store = await _store(relational_backend)
    first, second, third = _envelope("One"), _envelope("Two"), _envelope("Three")
    await store.append("x", "Order", [first, second, third], expected_version=0)

    assert _types(await store.stream_all(after_event_id=first.event_id, limit=1)) == ["Two"]
    assert _types(await store.stream_all(after_event_id=third.event_id)) == []
    with pytest.raises(ValueError, match="no-such-event"):
        await store.stream_all(after_event_id="no-such-event")


# ---------------------------------------------------------------------------------------------------------
# The ambient unit of work
# ---------------------------------------------------------------------------------------------------------


async def test_events_commit_and_roll_back_with_the_business_unit(relational_backend: RelationalBackend) -> None:
    """Proof p10 (F8): the order rolled back but its events stayed, a dual write."""
    store = await _store(relational_backend)
    await relational_backend.create_tables(orders)
    template = _template(store)

    with pytest.raises(ValueError, match="payment declined"):
        async with template.transaction():
            async with infrastructure_unit(store.engine) as session:
                await session.execute(insert(orders).values(id="order-1", name="first"))
            await store.append("order-1", "Order", [_envelope("OrderPlaced")], expected_version=0)
            raise ValueError("payment declined: the whole thing must roll back")

    async with store.engine.connect() as connection:
        assert (await connection.execute(select(func.count()).select_from(orders))).scalar() == 0
    assert await store.load("order-1") == []
    assert await _drain(store) == []

    async with template.transaction():
        async with infrastructure_unit(store.engine) as session:
            await session.execute(insert(orders).values(id="order-2", name="second"))
        await store.append("order-2", "Order", [_envelope("OrderPlaced")], expected_version=0)
        # Read your own writes inside the unit: the aggregate's events are there, not yet on the global stream.
        assert _types(await store.load("order-2")) == ["OrderPlaced"]
        assert await store.stream_all() == []

    assert _types(await _drain(store)) == ["OrderPlaced"]


async def test_an_append_rolled_back_to_a_savepoint_leaves_the_rest_of_the_unit_on_the_stream(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    template = _template(store)

    async with template.transaction():
        await store.append("kept", "Order", [_envelope("Kept")], expected_version=0)
        with pytest.raises(RuntimeError):
            async with template.transaction(propagation=Propagation.NESTED):
                await store.append("undone", "Order", [_envelope("Undone")], expected_version=0)
                raise RuntimeError("this step failed")
        await store.append("kept-too", "Order", [_envelope("KeptToo")], expected_version=0)

    events = await _drain(store)
    assert _types(events) == ["Kept", "KeptToo"]
    assert await store.load("undone") == []
    if store.position_strategy == "head-row":
        assert [event.global_position for event in events] == [1, 2]  # only committed events are numbered


async def test_a_store_first_used_inside_a_unit_that_has_written_joins_it(
    relational_backend: RelationalBackend,
) -> None:
    """Review of WP08: on SQLite through the datasource registry (its write units take the write lock at
    ``BEGIN``), a store built by hand and first used inside a unit that had written opened a write unit of its
    own to read the strategy its table recorded, which waits for that unit: ``IllegalTransactionStateError``.
    Once the tables and their head row exist, starting reads and writes nothing else."""
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    registry = DataSourceRegistry(relational_backend.config())
    try:
        datasource = registry.primary
        await SqlAlchemyEventStore(datasource, position_strategy=_STRATEGY).start()  # the tables and the head row
        await SqlAlchemyEventStore(datasource, position_strategy=_STRATEGY).append(
            "earlier", "Order", [_envelope("Earlier")], expected_version=0
        )
        async with datasource.engine.begin() as connection:
            await connection.run_sync(orders.create, checkfirst=True)

        store = SqlAlchemyEventStore(datasource, position_strategy=_STRATEGY)  # built by hand, not started
        async with TransactionTemplate(resolve_manager(datasource)).transaction():
            async with infrastructure_unit(datasource) as session:
                await session.execute(insert(orders).values(id="order-1", name="first"))
            await store.append("order-1", "Order", [_envelope("OrderPlaced")], expected_version=0)
        assert _types(await _drain(store)) == ["Earlier", "OrderPlaced"]
        async with datasource.engine.connect() as connection:
            assert (await connection.execute(select(func.count()).select_from(orders))).scalar() == 1
    finally:
        await registry.close()


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_business_units_that_append_do_not_wait_for_or_fail_on_one_another(
    relational_backend: RelationalBackend,
) -> None:
    """Units at REPEATABLE READ (MariaDB's default runs it with snapshot isolation, which fails a unit that
    locks a row another unit changed since it started reading) each read, then append, and commit in another
    order than they started: every one of them commits."""
    await _overlapping_units_commit(await _store(relational_backend))


async def _overlapping_units_commit(store: SqlAlchemyEventStore, units: int = 4) -> None:
    from pyfly.data.transaction import Isolation

    template = TransactionTemplate(
        SqlAlchemyTransactionManager.for_engine(store.engine), isolation=Isolation.REPEATABLE_READ
    )
    started = [asyncio.Event() for _ in range(units)]

    async def unit(index: int) -> None:
        async with template.transaction():
            await store.latest_version(f"rr-{index}")  # the unit has read: its snapshot is taken
            started[index].set()
            for event in started:
                await event.wait()
            await asyncio.sleep(0.02 * (units - index))  # the last to start commits first
            await store.append(f"rr-{index}", "Order", [_envelope(f"RR{index}")], expected_version=0)

    await asyncio.gather(*(detached(unit(index)) for index in range(units)))
    assert sorted(_types(await _drain(store))) == [f"RR{index}" for index in range(units)]


async def test_an_append_from_a_later_before_commit_callback_still_gets_a_position(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    template = _template(store)

    class AppendsBeforeCommit(TransactionSynchronizationAdapter):
        async def before_commit(self, read_only: bool) -> None:
            await store.append("outbox", "Relay", [_envelope("AppendedAtCommit")], expected_version=0)

    async with template.transaction():
        await store.append("order", "Order", [_envelope("Placed")], expected_version=0)
        register_synchronization(AppendsBeforeCommit())

    assert _types(await _drain(store)) == ["Placed", "AppendedAtCommit"]
    async with store.engine.connect() as connection:
        unpositioned = select(func.count()).select_from(event_store).where(event_store.c.global_position.is_(None))
        assert (await connection.execute(unpositioned)).scalar() == 0


# ---------------------------------------------------------------------------------------------------------
# Cost, schema and positions of earlier releases
# ---------------------------------------------------------------------------------------------------------


async def test_an_append_sends_its_events_in_one_insert(relational_backend: RelationalBackend) -> None:
    """C093: every event was an INSERT of its own. An append is the version check and one INSERT, whatever the
    position strategy: it never touches the head row."""
    store = await _store(relational_backend)
    with StatementCounter(store.engine) as counter:
        await store.append("batch", "Order", [_envelope(f"E{index}") for index in range(25)], expected_version=0)
    assert counter.counts() == {"SELECT": 1, "INSERT": 1}

    with StatementCounter(store.engine) as reads:
        assert len(await store.stream_all()) == 25
        assert len(await store.stream_all(after_position=await store.last_position())) == 0
    # MySQL and MariaDB also send their units' settings (SET TRANSACTION READ ONLY, the isolation level).
    statements = {verb: count for verb, count in reads.counts().items() if verb != "SET"}
    if store.position_strategy == "xid8":
        assert statements == {"SELECT": 3}  # nothing is written to read the stream
    else:
        # The first read numbers the 25 committed events in one round (a probe, the head row locked, the
        # events without a position, the head row moved on, their positions), then reads its page; the next
        # reads find nothing to number. On SQLite the round takes the write lock first: this engine is built by
        # hand, and its driver would begin the transaction only at the round's first write.
        expected = {"SELECT": 8, "UPDATE": 2}
        if relational_backend.dialect == "sqlite":
            expected["BEGIN"] = 1  # BEGIN IMMEDIATE
        assert statements == expected


async def test_the_tables_are_created_at_start_and_only_checked_without_ddl(
    relational_backend: RelationalBackend,
) -> None:
    unchecked = SqlAlchemyEventStore(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_event_store does not exist"):
        await unchecked.start()

    store = await _store(relational_backend, table_name="wp08_events", head_table_name="wp08_events_head")
    await store.append("custom", "Order", [_envelope("Stored")], expected_version=0)
    assert _types(await _drain(store)) == ["Stored"]
    checked = SqlAlchemyEventStore(
        store.engine, table_name="wp08_events", head_table_name="wp08_events_head", create_table=False
    )
    await checked.start()
    assert _types(await checked.stream_all()) == ["Stored"]


async def test_events_stored_by_an_earlier_release_get_positions_in_their_order(
    relational_backend: RelationalBackend,
) -> None:
    """Rows written before the table had a global position (an upgrade adds the column) are placed on the
    stream oldest first, before any event appended since: by the readers with head-row, at start with xid8."""
    engine = relational_backend.create_engine()
    await _store(relational_backend)  # the tables, as the upgrade's migration leaves them
    base = datetime(2026, 1, 1, tzinfo=UTC)
    legacy = [
        ("legacy-2", base + timedelta(seconds=2)),
        ("legacy-0", base),
        ("legacy-1", base + timedelta(seconds=1)),
    ]
    async with engine.begin() as connection:
        for name, occurred_at in legacy:
            envelope = _envelope(name.title(), occurred_at=occurred_at)
            envelope.aggregate_id, envelope.aggregate_type, envelope.sequence = name, "Order", 1
            await connection.execute(
                insert(event_store).values(
                    event_id=envelope.event_id,
                    aggregate_id=name,
                    aggregate_type="Order",
                    sequence=1,
                    event_type=envelope.event_type,
                    payload=envelope.to_json(),
                    metadata="{}",
                    occurred_at=occurred_at,
                    version=1,
                    tenant_id=None,
                    global_position=None,
                )
            )

    store = await _store(relational_backend)
    placed = select(event_store.c.aggregate_id).where(event_store.c.global_position.is_not(None))
    async with engine.connect() as connection:
        numbered = (await connection.execute(placed.order_by(event_store.c.global_position))).scalars().all()
    assert numbered == (["legacy-0", "legacy-1", "legacy-2"] if store.position_strategy == "xid8" else [])
    await store.append("new", "Order", [_envelope("New")], expected_version=0)
    assert _types(await _drain(store)) == ["Legacy-0", "Legacy-1", "Legacy-2", "New"]


@pytest.mark.backends(PG)
async def test_xid8_is_opt_in_on_postgresql_and_the_strategy_is_the_table_s(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend, "xid8")
    assert store.position_strategy == "xid8"

    # A store left to choose follows what the table recorded, and one configured for the head row refuses to
    # start on it: two strategies on one table would skip events.
    follower = await _store(relational_backend, "auto")
    assert follower.position_strategy == "xid8"
    mismatched = SqlAlchemyEventStore(store.engine, position_strategy="head-row")
    with pytest.raises(FrameworkSchemaError, match="xid8"):
        await mismatched.start()


async def test_the_head_row_strategy_is_the_default_on_every_backend(relational_backend: RelationalBackend) -> None:
    """Review of WP08: ``auto`` chose ``xid8`` on PostgreSQL, whose positions follow the order the writers took
    their transaction ids in, not the order they committed in."""
    store = await _store(relational_backend, "auto")
    assert store.position_strategy == "head-row"
    if relational_backend.dialect != "postgresql":
        with pytest.raises(ValueError, match="xid8"):
            await SqlAlchemyEventStore(store.engine, position_strategy="xid8").start()


async def test_a_missing_head_row_is_reported_and_a_start_brings_it_back(relational_backend: RelationalBackend) -> None:
    store = await _store(relational_backend, "head-row")
    await store.append("x", "Order", [_envelope("First")], expected_version=0)
    assert _types(await _drain(store)) == ["First"]
    async with store.engine.begin() as connection:
        await connection.execute(update(event_store_head).values(store="renamed-by-hand"))

    await store.append("x", "Order", [_envelope("Second")], expected_version=1)
    with pytest.raises(FrameworkSchemaError, match="head row"):
        await store.stream_all()
    await store.start()  # the head row again, taking the positions on from the highest one in the table
    events = await _drain(store)
    assert _types(events) == ["First", "Second"]
    assert [event.global_position for event in events] == [1, 2]


# The event table as release 26.09.07 created it, and the upgrade docs/modules/eventsourcing.md gives for each
# backend (PostgreSQL's is in test_eventsourcing_postgres_integration.py).
_EARLIER_EVENT_STORE = """
CREATE TABLE IF NOT EXISTS pyfly_event_store (
    event_id        VARCHAR(64) PRIMARY KEY,
    aggregate_id    VARCHAR(64) NOT NULL,
    aggregate_type  VARCHAR(255) NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      VARCHAR(255) NOT NULL,
    payload         TEXT NOT NULL,
    metadata        TEXT NOT NULL,
    occurred_at     TIMESTAMP NOT NULL,
    version         INTEGER NOT NULL,
    tenant_id       VARCHAR(64) NULL,
    UNIQUE (aggregate_id, sequence)
)
"""
_UPGRADES = {
    "sqlite": [
        "ALTER TABLE pyfly_event_store ADD COLUMN recorded_at DATETIME",
        "ALTER TABLE pyfly_event_store ADD COLUMN global_position BIGINT",
    ],
    "mysql": [
        "SET time_zone = '+00:00'",
        "ALTER TABLE pyfly_event_store ADD COLUMN recorded_at DATETIME(6) NULL, "
        "ADD COLUMN global_position BIGINT NULL, MODIFY occurred_at DATETIME(6) NOT NULL, "
        "MODIFY payload LONGTEXT NOT NULL, MODIFY metadata LONGTEXT NOT NULL",
    ],
}


@pytest.mark.backends("sqlite-file", MYSQL, MARIADB)
async def test_the_event_table_of_an_earlier_release_upgrades_with_the_documented_migration(
    relational_backend: RelationalBackend,
) -> None:
    from sqlalchemy import text

    engine = relational_backend.create_engine()
    base = datetime(2026, 1, 1, 12, 0)
    async with engine.begin() as connection:
        await connection.execute(text(_EARLIER_EVENT_STORE))
        for index, name in ((1, "Second"), (0, "First")):
            envelope = StoredEventEnvelope(event_type=name, aggregate_id=f"old-{index}", aggregate_type="Order")
            envelope.sequence, envelope.occurred_at = 1, (base + timedelta(minutes=index)).replace(tzinfo=UTC)
            await connection.execute(
                text(
                    "INSERT INTO pyfly_event_store (event_id, aggregate_id, aggregate_type, sequence, event_type, "
                    "payload, metadata, occurred_at, version, tenant_id) VALUES (:eid, :aid, 'Order', 1, :etype, "
                    ":payload, '{}', :occurred, 1, NULL)"
                ),
                {
                    "eid": envelope.event_id,
                    "aid": envelope.aggregate_id,
                    "etype": name,
                    "payload": envelope.to_json(),
                    "occurred": str(base + timedelta(minutes=index)),  # what sqlite3's default adapter wrote
                },
            )

    store = SqlAlchemyEventStore(engine)
    with pytest.raises(FrameworkSchemaError, match="pyfly_event_store.global_position does not exist"):
        await store.start()
    upgrade = _UPGRADES["sqlite" if relational_backend.dialect == "sqlite" else "mysql"]
    async with engine.connect() as connection:
        for statement in upgrade:
            await connection.execute(text(statement))
        await connection.commit()
    await store.start()  # creates the head row and the global_position index, and places the old events

    await store.append("new", "Order", [_envelope("New")], expected_version=0)
    events = await _drain(store)
    assert _types(events) == ["First", "Second", "New"]
    assert [event.global_position for event in events] == [1, 2, 3]


async def test_a_page_of_the_stream_is_read_through_the_global_position_index(
    relational_backend: RelationalBackend,
) -> None:
    """C070: every poll sorted the whole table (``Seq Scan`` + ``Sort`` on ``occurred_at``)."""
    from sqlalchemy import text

    store = await _store(relational_backend)
    for block in range(20):
        await store.append(f"bulk-{block}", "Order", [_envelope(f"B{i}") for i in range(100)], expected_version=0)
    middle = (await store.stream_all(after_position=0, limit=1000))[-1].global_position
    page = f"SELECT payload, global_position FROM pyfly_event_store WHERE global_position > {middle} "
    page += "ORDER BY global_position LIMIT 100"
    async with store.engine.connect() as connection:
        if relational_backend.dialect == "postgresql":
            await connection.execute(text("ANALYZE pyfly_event_store"))
            plan = "\n".join(str(row[0]) for row in await connection.execute(text(f"EXPLAIN {page}")))
        elif relational_backend.dialect == "sqlite":
            plan = "\n".join(str(row[-1]) for row in await connection.execute(text(f"EXPLAIN QUERY PLAN {page}")))
        else:
            await connection.execute(text("ANALYZE TABLE pyfly_event_store"))
            rows = (await connection.execute(text(f"EXPLAIN {page}"))).mappings().all()
            plan = "\n".join(str(row["key"]) for row in rows)
    assert "ix_pyfly_event_store_global_position" in plan, plan


async def test_skewed_clocks_do_not_reorder_the_stream(relational_backend: RelationalBackend) -> None:
    """C070: with a node's clock an hour ahead, the stream (ordered by ``occurred_at``) put an event before the
    one it followed, and a cursor between them lost it. Positions follow commit order, whatever the clocks."""
    store = await _store(relational_backend)
    now = datetime.now(UTC)
    await store.append(
        "acc", "Account", [_envelope("OpenedOnFastNode", occurred_at=now + timedelta(hours=1))], expected_version=0
    )
    await store.append("acc", "Account", [_envelope("DepositedOnSlowNode", occurred_at=now)], expected_version=1)
    await asyncio.sleep(0.01)  # SQLite's clock counts milliseconds
    await store.append(
        "other", "Account", [_envelope("OtherOnSlowNode", occurred_at=now - timedelta(hours=1))], expected_version=0
    )

    events = await _drain(store)
    assert _types(events) == ["OpenedOnFastNode", "DepositedOnSlowNode", "OtherOnSlowNode"]


async def test_a_backlog_larger_than_a_numbering_round_is_streamed_in_order(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    await store.append("big-a", "Order", [_envelope(f"A{i}") for i in range(1300)], expected_version=0)
    await store.append("big-b", "Order", [_envelope(f"B{i}") for i in range(1300)], expected_version=0)

    first_page = await store.stream_all(limit=500)
    assert len(first_page) == 500
    events = first_page + await _drain(store, first_page[-1].global_position or 0, limit=500)
    assert len(events) == 2600 and len({event.event_id for event in events}) == 2600
    assert [event.sequence for event in events if event.aggregate_id == "big-a"] == list(range(1, 1301))
    assert [event.sequence for event in events if event.aggregate_id == "big-b"] == list(range(1, 1301))
    if store.position_strategy == "head-row":
        assert [event.global_position for event in events] == list(range(1, 2601))


# ---------------------------------------------------------------------------------------------------------
# A backlog of events without a position (an earlier release's rows, or no reader for a while)
# ---------------------------------------------------------------------------------------------------------


async def _legacy_backlog(engine: Any, count: int, *, aggregates: int = 50) -> list[tuple[str, int]]:
    """*count* events as an earlier release left them (no ``recorded_at``, no position), inserted in an order
    unrelated to their ``occurred_at``; returns their ``(aggregate_id, sequence)`` in ``occurred_at`` order."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        envelope = _envelope(f"L{index}", occurred_at=base + timedelta(milliseconds=index))
        aggregate, sequence = f"legacy-{index % aggregates:03d}", index // aggregates + 1
        envelope.aggregate_id, envelope.aggregate_type, envelope.sequence = aggregate, "Order", sequence
        rows.append(
            {
                "event_id": envelope.event_id,
                "aggregate_id": aggregate,
                "aggregate_type": "Order",
                "sequence": sequence,
                "event_type": envelope.event_type,
                "payload": envelope.to_json(),
                "metadata": "{}",
                "occurred_at": envelope.occurred_at,
                "version": 1,
                "tenant_id": None,
                "recorded_at": None,
                "global_position": None,
            }
        )
    expected = [(row["aggregate_id"], row["sequence"]) for row in rows]
    random.Random(8).shuffle(rows)
    async with engine.begin() as connection:
        for start in range(0, count, 1000):
            await connection.execute(insert(event_store), rows[start : start + 1000])
    return expected


def _backlog_sorts(counter: StatementCounter) -> int:
    """The reads that sort the events without a position into their numbering order."""
    return sum(
        1
        for statement in counter.statements
        if statement.verb == "SELECT"
        and all(part in statement.sql.lower() for part in ("global_position is null", "order by coalesce("))
    )


def _backlog_checks(counter: StatementCounter) -> int:
    """The reads that check again which events of a backlog list still have no position."""
    return sum(
        1 for statement in counter.statements if statement.verb == "SELECT" and "event_id in (" in statement.sql.lower()
    )


async def test_a_backlog_is_sorted_once_while_it_is_numbered(relational_backend: RelationalBackend) -> None:
    """Review of WP08: every numbering round sorted all the events still without a position (no index can serve
    that order) to number the next 1000 of them, so a backlog of N events cost N²/1000 row visits: an upgraded
    table of a million events held every replica's start for 103 s on PostgreSQL. The backlog is now read in its
    order once (per window of 100 000 events) and numbered from that list; and with the head-row strategy a start
    leaves the numbering to the readers, which do it as they page."""
    engine = relational_backend.create_engine()
    await _store(relational_backend)
    expected = await _legacy_backlog(engine, 3500)

    store = SqlAlchemyEventStore(engine, position_strategy=_STRATEGY)
    with StatementCounter(engine) as starting:
        await store.start()
    with StatementCounter(engine) as reading:
        events = await _drain(store, limit=100)
    assert [(event.aggregate_id, event.sequence) for event in events] == expected
    assert [event.global_position for event in events] == list(range(1, 3501))
    # With no other store numbering meanwhile, the list is not checked again either.
    if store.position_strategy == "xid8":
        # Its readers never number: the start does, in four rounds, sorting the backlog once.
        assert (_backlog_sorts(starting), _backlog_checks(starting), starting.count("UPDATE") > 0) == (1, 0, True)
        assert reading.count("UPDATE") == 0
    else:
        assert starting.count("UPDATE") == 0
        assert (_backlog_sorts(reading), _backlog_checks(reading), reading.count("UPDATE") > 0) == (1, 0, True)


async def test_a_backlog_larger_than_a_window_is_sorted_once_per_window(
    relational_backend: RelationalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyfly.eventsourcing.store as store_module

    monkeypatch.setattr(store_module, "_NUMBERING_WINDOW", 1500)
    engine = relational_backend.create_engine()
    store = SqlAlchemyEventStore(engine, position_strategy="head-row")
    await store.start()
    expected = await _legacy_backlog(engine, 3500)
    with StatementCounter(engine) as numbering:
        assert await store.last_position() == 3500  # numbers every committed event
    assert _backlog_sorts(numbering) == 3
    events = await _drain(store, limit=1000)
    assert [(event.aggregate_id, event.sequence) for event in events] == expected
    assert [event.global_position for event in events] == list(range(1, 3501))


async def test_events_another_store_numbered_meanwhile_are_not_numbered_twice(
    relational_backend: RelationalBackend,
) -> None:
    """A store works through the backlog list it read; another store (another replica) may number part of that
    list meanwhile: the list's events are checked again, and those numbered are passed over."""
    engine = relational_backend.create_engine()
    await _store(relational_backend, "head-row")
    expected = await _legacy_backlog(engine, 2500)
    reader = await _store(relational_backend, "head-row")
    first = await reader.stream_all(limit=100)  # reads the backlog's order and numbers its first round
    assert [(event.aggregate_id, event.sequence) for event in first] == expected[:100]

    other = await _store(relational_backend, "head-row")
    await other.append("late", "Order", [_envelope("Late")], expected_version=0)
    assert await other.last_position() == 2501  # the rest of the backlog, then the late event
    await other.append("later", "Order", [_envelope("Later")], expected_version=0)

    with StatementCounter(reader.engine) as reading:
        events = first + await _drain(reader, first[-1].global_position or 0, limit=100)
    assert [(event.aggregate_id, event.sequence) for event in events] == [*expected, ("late", 1), ("later", 1)]
    assert [event.global_position for event in events] == list(range(1, 2503))
    assert _backlog_checks(reading) == 3  # the reader's list, 1500 events after its first round, checked again


async def test_a_numbering_round_that_fails_is_numbered_again_in_order(relational_backend: RelationalBackend) -> None:
    from sqlalchemy import event as sqlalchemy_event

    engine = relational_backend.create_engine()
    store = SqlAlchemyEventStore(engine, position_strategy="head-row")
    await store.start()
    expected = await _legacy_backlog(engine, 2500)
    first = await store.stream_all(limit=1000)

    def fail_the_positions(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.lstrip().upper().startswith("UPDATE PYFLY_EVENT_STORE SET GLOBAL_POSITION"):
            raise RuntimeError("the numbering unit fails")

    sqlalchemy_event.listen(engine.sync_engine, "before_cursor_execute", fail_the_positions)
    try:
        with pytest.raises(RuntimeError, match="numbering unit fails"):
            await store.stream_all(after_position=1000, limit=1000)
    finally:
        sqlalchemy_event.remove(engine.sync_engine, "before_cursor_execute", fail_the_positions)

    events = first + await _drain(store, 1000, limit=1000)
    assert [(event.aggregate_id, event.sequence) for event in events] == expected
    assert [event.global_position for event in events] == list(range(1, 2501))


# ---------------------------------------------------------------------------------------------------------
# Numbering rounds side by side
# ---------------------------------------------------------------------------------------------------------

# A store on an engine built by hand (on SQLite: no BEGIN recipe, the driver defers BEGIN until the first write)
# and one on the datasource registry's engine (on SQLite: the recipe, whose write units begin IMMEDIATE).
_THROUGH = pytest.mark.parametrize("through", ["engine", "registry"])


@contextlib.asynccontextmanager
async def _store_through(backend: RelationalBackend, through: str) -> AsyncIterator[SqlAlchemyEventStore]:
    if through == "engine":
        yield await _store(backend)
        return
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    registry = DataSourceRegistry(backend.config())
    try:
        store = SqlAlchemyEventStore(registry.primary, position_strategy=_STRATEGY)
        await store.start()
        yield store
    finally:
        await registry.close()


async def _head_and_highest(store: SqlAlchemyEventStore) -> tuple[int, int]:
    """The head row's position and the highest position given out."""
    async with store.engine.connect() as connection:
        head = (
            await connection.execute(
                select(event_store_head.c.position).where(event_store_head.c.store == "pyfly_event_store")
            )
        ).scalar_one()
        highest = (await connection.execute(select(func.coalesce(func.max(event_store.c.global_position), 0)))).scalar()
    return int(head), int(highest or 0)


def _head_row_moves(caplog: pytest.LogCaptureFixture) -> int:
    return sum(1 for record in caplog.records if record.getMessage() == "event_store_head_row_moved")


@_THROUGH
async def test_readers_side_by_side_number_each_event_once_and_the_head_row_never_goes_back(
    relational_backend: RelationalBackend, through: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Final review of WP08: a numbering round locked the head row with ``SELECT ... FOR UPDATE``, which SQLite
    ignores, and on a SQLite engine built by hand the driver defers ``BEGIN`` until the first write. Two rounds
    read the same head row, and the later one moved it back below positions already given out; every round after
    that gave out positions that existed (UNIQUE global_position), so the stream failed for good, after a restart
    too. On SQLite a round now takes the write lock before it reads the head row, and on every backend it moves
    the head row only from the position it read: readers side by side queue, never number an event twice, and all
    catch up."""
    caplog.set_level(logging.WARNING, logger="pyfly.eventsourcing.store")
    writers, appends, readers = 4, 40, 4
    total = writers * appends
    pages = random.Random(24)
    async with _store_through(relational_backend, through) as store:

        async def write(writer: int) -> None:
            for index in range(appends):
                await store.append(f"w{writer}-{index}", "Order", [_envelope(f"W{writer}")], expected_version=0)
                await asyncio.sleep(pages.random() * 0.002)

        async def read() -> list[tuple[str, int]]:
            seen: list[StoredEventEnvelope] = []
            after = 0
            while len(seen) < total:
                page = await store.stream_all(after_position=after, limit=pages.randint(1, 5))
                if not page:
                    await asyncio.sleep(0.001)
                    continue
                seen.extend(page)
                after = page[-1].global_position or after
            return [(event.event_id, event.global_position or 0) for event in seen]

        async with asyncio.timeout(120):
            _, views = await asyncio.gather(
                asyncio.gather(*(write(writer) for writer in range(writers))),
                asyncio.gather(*(read() for _ in range(readers))),
            )

        first = views[0]
        assert len(first) == total and len({event_id for event_id, _ in first}) == total
        assert all(view == first for view in views)  # every reader: every event once, at one position
        positions = [position for _, position in first]
        assert positions == sorted(positions) and len(set(positions)) == total
        if store.position_strategy == "head-row":
            assert positions == list(range(1, total + 1))
            assert await _head_and_highest(store) == (total, total)

        # The stream goes on, for this store and for one started again on the table.
        await store.append("after", "Order", [_envelope("After")], expected_version=0)
        assert _types(await store.stream_all(after_position=positions[-1])) == ["After"]
        restarted = SqlAlchemyEventStore(store.engine, position_strategy=_STRATEGY)
        await restarted.start()
        assert _types(await restarted.stream_all(after_position=positions[-1])) == ["After"]
    assert _head_row_moves(caplog) == 0  # the rounds queued on the head row's lock: none ran twice


def _move_the_head_row_in_the_round(times: int) -> Callable[..., None]:
    """A ``before_execute`` listener that, before a numbering round moves the head row (its first *times* rounds),
    does what another store's round would do if the round's lock did not keep it out: it numbers a newer event (at
    the head row's position plus one) and moves the head row to it, in the round's own transaction."""
    left, moving = [times], [False]

    def move(conn: Any, clauseelement: Any, *_args: Any) -> None:
        if moving[0] or not left[0]:
            return
        if not (isinstance(clauseelement, Update) and clauseelement.table.name == event_store_head.name):
            return
        left[0] -= 1
        moving[0] = True  # its own UPDATE of the head row is not a round's
        try:
            _number_elsewhere(conn)
        finally:
            moving[0] = False

    return move


def _number_elsewhere(conn: Any) -> None:
    """Another store's numbering round, as the listener above plays it: a newer event at the head row's position
    plus one, and the head row moved to it."""
    head = conn.execute(
        select(event_store_head.c.position).where(event_store_head.c.store == "pyfly_event_store")
    ).scalar_one()
    elsewhere = _envelope("NumberedElsewhere")
    conn.execute(
        insert(event_store).values(
            event_id=elsewhere.event_id,
            aggregate_id=f"elsewhere-{elsewhere.event_id}",
            aggregate_type="Order",
            sequence=1,
            event_type=elsewhere.event_type,
            payload=elsewhere.to_json(),
            metadata="{}",
            occurred_at=elsewhere.occurred_at,
            version=1,
            global_position=int(head) + 1,
        )
    )
    conn.execute(
        update(event_store_head).where(event_store_head.c.store == "pyfly_event_store").values(position=int(head) + 1)
    )


@_THROUGH
async def test_a_numbering_round_that_finds_the_head_row_moved_runs_again(
    relational_backend: RelationalBackend, through: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The head row moves only from the position the round read (``UPDATE ... WHERE position = :read``), which
    holds on every backend whatever keeps other rounds out. The round's lock does (``FOR UPDATE``; SQLite's write
    lock, taken first), so the test moves the head row inside the round, as a round that took no lock would have
    seen another store do. Without the fence the round went on from the position it read: it gave the newer
    event's position to another event and failed, or moved the head row back. The round is rolled back with what
    it saw and runs again."""
    from sqlalchemy import event as sqlalchemy_event

    caplog.set_level(logging.WARNING, logger="pyfly.eventsourcing.store")
    async with _store_through(relational_backend, through) as store:
        if store.position_strategy != "head-row":
            return
        await store.append("acc", "Account", [_envelope("Opened"), _envelope("Deposited")], expected_version=0)
        move = _move_the_head_row_in_the_round(1)
        sqlalchemy_event.listen(store.engine.sync_engine, "before_execute", move)
        try:
            events = await _drain(store)
        finally:
            sqlalchemy_event.remove(store.engine.sync_engine, "before_execute", move)

        assert _head_row_moves(caplog) == 1
        assert [(event.event_type, event.global_position) for event in events] == [("Opened", 1), ("Deposited", 2)]
        assert await _head_and_highest(store) == (2, 2)  # the move went with the round it was made in


async def test_a_head_row_that_keeps_moving_under_the_numbering_fails_the_read_and_says_so(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """A round that finds the head row moved runs again, a bounded number of times: a head row that moves under
    every round (something writes it without its lock) fails the read, with an error that says so, rather than
    retrying forever or giving events positions another event has."""
    from sqlalchemy import event as sqlalchemy_event

    from pyfly.kernel.exceptions import ConcurrencyException

    caplog.set_level(logging.WARNING, logger="pyfly.eventsourcing.store")
    store = await _store(relational_backend, "head-row")
    await store.append("acc", "Account", [_envelope("Opened")], expected_version=0)
    move = _move_the_head_row_in_the_round(100)
    sqlalchemy_event.listen(store.engine.sync_engine, "before_execute", move)
    try:
        with pytest.raises(ConcurrencyException, match="head row of event table pyfly_event_store kept moving"):
            await store.stream_all()
    finally:
        sqlalchemy_event.remove(store.engine.sync_engine, "before_execute", move)

    assert _head_row_moves(caplog) == 5
    assert [(event.event_type, event.global_position) for event in await _drain(store)] == [("Opened", 1)]
    assert await _head_and_highest(store) == (1, 1)


async def test_a_reader_behind_a_full_numbered_page_reads_it_without_numbering(
    relational_backend: RelationalBackend,
) -> None:
    """Final review of WP08: a page read ran a numbering round (the head row locked and written) whenever a
    committed event had no position, even with a full numbered page after the reader's cursor, so a runner
    catching up over a numbered history under steady writes took the head row's lock and wrote on every page.
    The positions follow one another without gaps: a head row at or past the page's end means the page is there."""
    store = await _store(relational_backend, "head-row")
    await store.append("numbered", "Order", [_envelope(f"N{index}") for index in range(10)], expected_version=0)
    assert len(await _drain(store)) == 10
    await store.append("new", "Order", [_envelope("New")], expected_version=0)  # committed, no position yet

    with StatementCounter(store.engine) as behind:
        page = await store.stream_all(after_position=2, limit=5)
    assert [(event.event_type, event.global_position) for event in page] == [(f"N{i}", i + 1) for i in range(2, 7)]
    assert (behind.count("UPDATE"), behind.count("SELECT")) == (0, 2)  # the probe, the page

    with StatementCounter(store.engine) as at_the_end:
        tail = await store.stream_all(after_position=8, limit=5)
    assert [(event.event_type, event.global_position) for event in tail] == [("N8", 9), ("N9", 10), ("New", 11)]
    assert at_the_end.count("UPDATE") == 2  # the page reaches past the head row: the new event is numbered


# The set-based numbering docs/modules/eventsourcing.md gives for a large table of an earlier release.
_NUMBER_EARLIER_EVENTS = {
    "postgresql": """
UPDATE pyfly_event_store AS e SET global_position = n.position
FROM (SELECT event_id, ROW_NUMBER() OVER (ORDER BY occurred_at, aggregate_id, sequence) AS position
      FROM pyfly_event_store) AS n
WHERE e.event_id = n.event_id
""",
    "mysql": """
UPDATE pyfly_event_store AS e
JOIN (SELECT event_id, ROW_NUMBER() OVER (ORDER BY occurred_at, aggregate_id, sequence) AS position
      FROM pyfly_event_store) AS n ON e.event_id = n.event_id
SET e.global_position = n.position
""",
}
_MOVE_THE_HEAD_ROW_ON = """
UPDATE pyfly_event_store_head SET position = (SELECT MAX(global_position) FROM pyfly_event_store)
WHERE store = 'pyfly_event_store'
"""


async def test_the_documented_sql_numbers_an_earlier_release_s_events_before_the_first_start(
    relational_backend: RelationalBackend,
) -> None:
    """The set-based numbering the upgrade notes give for a large table, where a store has already started on it
    (its head row is there, at 0): the head row takes the positions on from the highest one."""
    from sqlalchemy import text

    engine = relational_backend.create_engine()
    await _store(relational_backend)
    expected = await _legacy_backlog(engine, 1200)
    numbering = _NUMBER_EARLIER_EVENTS["mysql" if relational_backend.dialect in ("mysql", "mariadb") else "postgresql"]
    async with engine.begin() as connection:
        await connection.execute(text(numbering))
        await connection.execute(text(_MOVE_THE_HEAD_ROW_ON))

    store = SqlAlchemyEventStore(engine, position_strategy=_STRATEGY)
    with StatementCounter(engine) as reading:
        await store.start()
        await store.append("new", "Order", [_envelope("New")], expected_version=0)
        events = await _drain(store, limit=500)
    assert [(event.aggregate_id, event.sequence) for event in events] == [*expected, ("new", 1)]
    positions = [event.global_position or 0 for event in events]
    assert positions[:-1] == list(range(1, 1201)) and positions[-1] > 1200
    assert _backlog_sorts(reading) == (0 if store.position_strategy == "xid8" else 1)  # only the new event is left


# ---------------------------------------------------------------------------------------------------------
# PostgreSQL's xid8 accelerator
# ---------------------------------------------------------------------------------------------------------

_XID8_SCENARIOS: tuple[Callable[[RelationalBackend], Awaitable[None]], ...] = (
    test_events_round_trip_with_their_sequence_metadata_and_global_position,
    test_an_envelope_equals_itself_read_back_whatever_its_global_position,
    test_the_global_stream_follows_commit_order_not_the_envelope_clock,
    test_events_that_share_a_timestamp_are_each_streamed_once,
    test_an_append_waits_for_no_other_unit_and_a_late_commit_is_not_skipped_on_the_stream,
    test_concurrent_appends_get_distinct_increasing_positions,
    test_concurrent_appends_to_one_aggregate_let_one_win,
    test_an_aggregate_s_events_stream_in_sequence_order_across_units,
    test_after_event_id_still_pages_and_an_unknown_id_is_refused,
    test_events_commit_and_roll_back_with_the_business_unit,
    test_an_append_rolled_back_to_a_savepoint_leaves_the_rest_of_the_unit_on_the_stream,
    test_a_store_first_used_inside_a_unit_that_has_written_joins_it,
    test_business_units_that_append_do_not_wait_for_or_fail_on_one_another,
    test_an_append_from_a_later_before_commit_callback_still_gets_a_position,
    test_an_append_sends_its_events_in_one_insert,
    test_the_tables_are_created_at_start_and_only_checked_without_ddl,
    test_events_stored_by_an_earlier_release_get_positions_in_their_order,
    test_a_page_of_the_stream_is_read_through_the_global_position_index,
    test_skewed_clocks_do_not_reorder_the_stream,
    test_a_backlog_larger_than_a_numbering_round_is_streamed_in_order,
    test_a_backlog_is_sorted_once_while_it_is_numbered,
    test_the_documented_sql_numbers_an_earlier_release_s_events_before_the_first_start,
)


@pytest.mark.backends(PG)
@pytest.mark.parametrize("scenario", _XID8_SCENARIOS, ids=lambda scenario: scenario.__name__.removeprefix("test_"))
async def test_the_xid8_strategy_on_postgresql(
    relational_backend: RelationalBackend,
    scenario: Callable[[RelationalBackend], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scenarios above run every lane with the default strategy (head-row, PostgreSQL's too); PostgreSQL runs
    them again with its opt-in ``xid8`` accelerator."""
    monkeypatch.setattr(sys.modules[__name__], "_STRATEGY", "xid8")
    await scenario(relational_backend)
