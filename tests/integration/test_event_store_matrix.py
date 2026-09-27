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
every event a global position that follows commit order (a head-row counter that numbers the committed events
when the stream is read, or on PostgreSQL the writer's ``xid8`` below the readers' snapshot horizon), pages by
it, and joins the ambient unit of work, so an aggregate's events commit or roll back with the rest of the
business transaction.

The sqlite-file lane runs in the fast suite; PostgreSQL (both position strategies), MySQL and MariaDB run with
``-m integration``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Column, MetaData, String, Table, func, insert, select, update

from pyfly.data.relational.framework_schema import FrameworkSchemaError, event_store, event_store_head
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    Propagation,
    TransactionSynchronizationAdapter,
    TransactionTemplate,
    detached,
    infrastructure_unit,
    register_synchronization,
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


async def _store(backend: RelationalBackend, strategy: str = "auto", **options: object) -> SqlAlchemyEventStore:
    store = SqlAlchemyEventStore(backend.create_engine(), position_strategy=strategy, **options)  # type: ignore[arg-type]
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


@pytest.mark.backends(PG)
async def test_the_head_row_strategy_does_not_skip_a_late_commit_on_postgresql(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend, "head-row")
    assert store.position_strategy == "head-row"
    await _late_commit_scenario(store)


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


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_business_units_that_append_do_not_wait_for_or_fail_on_one_another(
    relational_backend: RelationalBackend,
) -> None:
    """Units at REPEATABLE READ (MariaDB's default runs it with snapshot isolation, which fails a unit that
    locks a row another unit changed since it started reading) each read, then append, and commit in another
    order than they started: every one of them commits."""
    await _overlapping_units_commit(await _store(relational_backend))
    if relational_backend.dialect == "postgresql":
        await _overlapping_units_commit(
            await _store(relational_backend, "head-row", table_name="wp08_rr_events", head_table_name="wp08_rr_head")
        )


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
        # reads find nothing to number.
        assert statements == {"SELECT": 8, "UPDATE": 2}


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


async def test_events_stored_by_an_earlier_release_get_positions_in_their_order_at_start(
    relational_backend: RelationalBackend,
) -> None:
    """Rows written before the table had a global position (an upgrade adds the column) are placed on the
    stream at start, oldest first, before any event appended since."""
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
    placed = select(event_store.c.aggregate_id).order_by(event_store.c.global_position)
    async with engine.connect() as connection:
        assert (await connection.execute(placed)).scalars().all() == ["legacy-0", "legacy-1", "legacy-2"]
    await store.append("new", "Order", [_envelope("New")], expected_version=0)
    assert _types(await _drain(store)) == ["Legacy-0", "Legacy-1", "Legacy-2", "New"]


@pytest.mark.backends(PG)
async def test_postgresql_uses_the_xid8_guard_and_the_strategy_is_the_table_s(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    assert store.position_strategy == "xid8"

    # A store configured for the head row on the same table follows what the table recorded when it is
    # left to choose, and refuses to start when told otherwise: two strategies on one table would skip events.
    follower = await _store(relational_backend)
    assert follower.position_strategy == "xid8"
    mismatched = SqlAlchemyEventStore(store.engine, position_strategy="head-row")
    with pytest.raises(FrameworkSchemaError, match="xid8"):
        await mismatched.start()


async def test_a_head_row_strategy_is_the_default_where_postgresql_s_guard_is_missing(
    relational_backend: RelationalBackend,
) -> None:
    store = await _store(relational_backend)
    expected = "xid8" if relational_backend.dialect == "postgresql" else "head-row"
    assert store.position_strategy == expected
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
