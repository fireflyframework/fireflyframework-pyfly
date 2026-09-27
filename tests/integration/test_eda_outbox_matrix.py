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
"""The transactional outbox on every relational lane (WP09: F8, C009, C010, C064, C146, C149).

- F8 / proof p10: the EDA "outbox" wrote on a pool of its own, so a business transaction that rolled back
  left its event published. A publish now joins the unit of work: rolled back, no event; committed, delivered.
- C009 / C010: consumers read ``id > cursor`` over a sequence, so a row that committed after a higher id was
  consumed was skipped forever (about 0.2 % of events under ordinary concurrency). Deliveries are now claimed
  by state: out-of-order commits and concurrent publishers lose nothing, and two relays of one group never
  take the same delivery.
- C064: a handler that kept failing stalled its whole group and re-ran the handlers that had succeeded. Each
  subscription is now settled on its own, attempted a bounded number of times, then dead-lettered.
- C146 / C149: the outbox was never pruned, and a new group replayed the whole history.

SQLite file (foreign keys on), PostgreSQL, MySQL 8 and MariaDB 11.
"""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.outbox import Outbox, OutboxRelay, OutboxTables, Retention
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from pyfly.messaging.listener_container import FixedBackOff, RetryPolicy
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend

TABLES = OutboxTables.named()


class OutboxOrder(Base):
    __tablename__ = "wp09_outbox_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class Recorder:
    """A handler that records the events it saw (and can be told to fail for some of them)."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.seen: list[EventEnvelope] = []
        self.fail_on = fail_on or set()
        self.attempts = 0

    async def __call__(self, envelope: EventEnvelope) -> None:
        self.attempts += 1
        if envelope.event_type in self.fail_on:
            raise RuntimeError(f"cannot handle {envelope.event_type}")
        self.seen.append(envelope)

    def ids(self) -> list[Any]:
        return [envelope.payload["n"] for envelope in self.seen]


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(max_attempts=attempts, backoff=FixedBackOff(0.0))


async def _bus(engine: AsyncEngine, **options: Any) -> DatabaseEventBus:
    options.setdefault("retry", _retry(3))
    bus = DatabaseEventBus(engine, notify=False, **options)
    await bus.outbox.start()
    return bus


async def _drain(relay: OutboxRelay, *, quiet_rounds: int = 2) -> None:
    """Run rounds until *quiet_rounds* in a row claim nothing."""
    quiet = 0
    for _ in range(500):
        if await relay.run_once() == 0:
            quiet += 1
            if quiet >= quiet_rounds:
                return
        else:
            quiet = 0
    raise AssertionError("the relay never went quiet")


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(text(table)))).scalar_one())


async def test_a_publish_is_part_of_the_unit_of_work(relational_backend: RelationalBackend) -> None:
    """Proof p10: after a rollback orders=0 but events=1. Now the event goes (or stays) with the unit."""
    engine = relational_backend.create_engine()
    await relational_backend.create_tables(OutboxOrder)
    bus = await _bus(engine, group="billing")
    received = Recorder()
    bus.subscribe("order.*", received)
    await bus.relay.run_once()  # registers the group
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError, match="payment declined"):
        async with template.transaction() as unit:
            assert unit is not None
            unit.resource.add(OutboxOrder(name="order-1"))
            await bus.publish("orders", "order.placed", {"n": 1})
            raise RuntimeError("payment declined")
    assert await _count(engine, "wp09_outbox_order") == 0
    assert await _count(engine, TABLES.events.name) == 0
    assert await _count(engine, TABLES.deliveries.name) == 0

    async with template.transaction() as unit:
        assert unit is not None
        unit.resource.add(OutboxOrder(name="order-2"))
        await bus.publish("orders", "order.placed", {"n": 2})
    await _drain(bus.relay)
    assert await _count(engine, "wp09_outbox_order") == 1
    assert received.ids() == [2]


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_an_event_that_commits_after_a_later_one_is_still_delivered(
    relational_backend: RelationalBackend,
) -> None:
    """C009/C010, case (a): a writer holds its INSERT in a transaction while a later publish commits and is
    consumed. The cursor then stood past the held row's id, and the row was never read."""
    engine = relational_backend.create_engine()
    bus = await _bus(engine, group="audit")
    received = Recorder()
    bus.subscribe("*", received)
    await bus.relay.run_once()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    held = asyncio.Event()
    release = asyncio.Event()

    async def slow_writer() -> None:
        async with template.transaction():
            await bus.publish("audit", "slow", {"n": "slow"})  # takes the lower id
            held.set()
            await release.wait()

    writer = asyncio.create_task(slow_writer())
    await held.wait()
    await bus.publish("audit", "fast", {"n": "fast"})  # a higher id, committed first
    await _drain(bus.relay)
    assert received.ids() == ["fast"]

    release.set()
    await writer
    await _drain(bus.relay)
    assert received.ids() == ["fast", "slow"]
    ids = [envelope.event_id for envelope in received.seen]
    assert len(set(ids)) == 2


async def test_concurrent_publishers_lose_nothing(relational_backend: RelationalBackend) -> None:
    """C009/C010, case (c): concurrent publishes, each in a unit of its own that commits after a random
    delay, with a relay draining meanwhile. Every event is delivered, once."""
    engine = relational_backend.create_engine()
    bus = await _bus(engine, group="ledger")
    received = Recorder()
    bus.subscribe("*", received)
    await bus.relay.run_once()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    rng = random.Random(9)
    tasks = 6 if relational_backend.is_embedded else 12
    per_task = 15

    async def publisher(number: int) -> None:
        for index in range(per_task):
            delay = rng.random() / 200
            async with template.transaction():
                await bus.publish("ledger", "entry", {"n": f"{number}-{index}"})
                await asyncio.sleep(delay)

    async def drainer(stop: asyncio.Event) -> None:
        while not stop.is_set():
            await bus.relay.run_once()
            await asyncio.sleep(0.001)

    stop = asyncio.Event()
    draining = asyncio.create_task(drainer(stop))
    await asyncio.gather(*(publisher(number) for number in range(tasks)))
    stop.set()
    await draining
    await _drain(bus.relay)

    expected = {f"{number}-{index}" for number in range(tasks) for index in range(per_task)}
    assert sorted(received.ids()) == sorted(expected)  # each exactly once
    assert await _count(engine, TABLES.deliveries.name) == 0


async def test_two_relays_of_one_group_never_take_the_same_delivery(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    first = await _bus(engine, group="workers", batch_size=5)
    second = await _bus(engine, group="workers", batch_size=5)
    seen: list[tuple[str, Any]] = []

    def handler(name: str) -> Any:
        async def handle(envelope: EventEnvelope) -> None:
            seen.append((name, envelope.payload["n"]))
            await asyncio.sleep(0.001)

        return handle

    first.subscribe("job", handler("first"))
    second.subscribe("job", handler("second"))
    await first.relay.run_once()
    for number in range(60):
        await first.publish("jobs", "job", {"n": number})

    await asyncio.gather(_drain(first.relay), _drain(second.relay))

    numbers = sorted(n for _name, n in seen)
    assert numbers == list(range(60))  # none twice, none lost
    assert {name for name, _n in seen} <= {"first", "second"}


async def test_a_failing_subscription_is_isolated_then_dead_lettered(relational_backend: RelationalBackend) -> None:
    """C064: the poison event stalled the group and re-ran the healthy handler on every retry."""
    engine = relational_backend.create_engine()
    bus = await _bus(engine, group="mailers", retry=_retry(3))
    healthy = Recorder()
    failing = Recorder(fail_on={"poison"})
    bus.subscribe("*", healthy)
    bus.subscribe("poison", failing)
    await bus.relay.run_once()

    await bus.publish("mail", "poison", {"n": "poison"})
    for number in range(3):
        await bus.publish("mail", "welcome", {"n": number})
    await _drain(bus.relay)

    assert healthy.ids() == ["poison", 0, 1, 2]  # the poison event reached the healthy handler once
    assert failing.attempts == 3
    assert bus.relay.counters.dead_lettered == 1
    letters = await bus.outbox.dead_letters("mailers")
    assert len(letters) == 1
    letter = letters[0]
    assert letter.event.event_type == "poison"
    assert letter.event.payload == {"n": "poison"}
    assert letter.attempts == 3
    assert letter.error_type == "RuntimeError"
    assert letter.subscription is not None and letter.subscription.startswith("poison ")
    assert await _count(engine, TABLES.deliveries.name) == 0


async def test_error_strategies_let_go_or_dead_letter_at_once(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    continuing = await _bus(engine, group="lenient", error_strategy=ErrorStrategy.LOG_AND_CONTINUE)
    fail_fast = await _bus(engine, group="strict", error_strategy=ErrorStrategy.FAIL_FAST)
    lenient, strict = Recorder(fail_on={"bad"}), Recorder(fail_on={"bad"})
    continuing.subscribe("*", lenient)
    fail_fast.subscribe("*", strict)
    await continuing.relay.run_once()
    await fail_fast.relay.run_once()

    await continuing.publish("x", "bad", {"n": 1})
    await asyncio.gather(_drain(continuing.relay), _drain(fail_fast.relay))

    assert (lenient.attempts, strict.attempts) == (1, 1)
    assert await continuing.outbox.dead_letters("lenient") == []
    assert len(await fail_fast.outbox.dead_letters("strict")) == 1


async def test_retention_deletes_what_every_group_handled_and_nothing_else(
    relational_backend: RelationalBackend,
) -> None:
    """C146: every event stayed forever. It is deleted once every group it was owed to handled it."""
    engine = relational_backend.create_engine()
    handled = await _bus(engine, group="handled", destinations=["orders", "audit"])
    idle = await _bus(engine, group="idle", destinations=["audit"])
    handled.subscribe("*", Recorder())
    idle.subscribe("*", Recorder())
    await handled.relay.run_once()
    await idle.outbox.register("idle", ["audit"])

    for number in range(3):
        await handled.publish("orders", "order.placed", {"n": number})
    await handled.publish("audit", "login", {"n": "a"})  # also owed to the idle group
    await _drain(handled.relay)

    result = await handled.outbox.prune(Retention(delivered=timedelta(0)))
    assert result.delivered == 3
    assert await _count(engine, TABLES.events.name) == 1  # the audit event the idle group still owes
    pending = await handled.outbox.pending("idle")
    assert [p.envelope.event_type for p in pending] == ["login"]

    expired = await handled.outbox.prune(Retention(delivered=None, max_age=timedelta(0)))
    assert (expired.expired, expired.undelivered) == (1, 1)
    assert await _count(engine, TABLES.events.name) == 0
    assert await handled.outbox.pending("idle") == []


async def test_a_new_group_starts_at_the_latest_event_unless_told_the_earliest(
    relational_backend: RelationalBackend,
) -> None:
    """C149: a new group replayed the whole history."""
    engine = relational_backend.create_engine()
    first = await _bus(engine, group="first")
    first.subscribe("*", Recorder())
    await first.relay.run_once()
    await first.publish("orders", "old", {"n": "old"})

    latest = await _bus(engine, group="latest")
    earliest = await _bus(engine, group="earliest", start_position="earliest")
    late, early = Recorder(), Recorder()
    latest.subscribe("*", late)
    earliest.subscribe("*", early)
    await latest.relay.run_once()
    await earliest.relay.run_once()
    await first.publish("orders", "new", {"n": "new"})
    await asyncio.gather(_drain(latest.relay), _drain(earliest.relay))

    assert late.ids() == ["new"]
    assert early.ids() == ["old", "new"]


async def test_a_group_registered_for_some_destinations_gets_only_those(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    bus = await _bus(engine, group="payments", destinations=["payments"])
    received = Recorder()
    bus.subscribe("*", received)
    await bus.relay.run_once()
    await bus.publish("orders", "order.placed", {"n": "order"})
    await bus.publish("payments", "payment.taken", {"n": "payment"})
    await _drain(bus.relay)
    assert received.ids() == ["payment"]


async def test_a_claim_whose_lease_ended_is_taken_again_and_the_late_settle_is_refused(
    relational_backend: RelationalBackend,
) -> None:
    """A relay that dies (or hangs past its lease) leaves its deliveries to be claimed again."""
    engine = relational_backend.create_engine()
    outbox = Outbox(engine)
    await outbox.start()
    await outbox.register("g", None)
    await outbox.append(EventEnvelope("e", {"n": 1}, "d"))

    first = await outbox.claim("g", limit=10, lease=timedelta(seconds=-1), owner="dead-node")
    assert len(first) == 1
    second = await outbox.claim("g", limit=10, lease=timedelta(minutes=5), owner="live-node")
    assert [d.outbox_id for d in second] == [first[0].outbox_id]
    assert second[0].attempts == 2

    assert await outbox.settle(first[0]) is False  # the dead node's claim is gone
    assert await outbox.settle(second[0]) is True
    assert await outbox.pending("g") == []


async def test_a_hung_handler_is_cancelled_after_its_timeout_and_counts_as_a_failure(
    relational_backend: RelationalBackend,
) -> None:
    """A stalled handler neither stalls its group nor keeps its delivery: it fails after handler_timeout."""
    engine = relational_backend.create_engine()
    bus = await _bus(engine, group="stalls", handler_timeout=0.2, retry=_retry(2))
    seen: list[Any] = []

    async def handler(envelope: EventEnvelope) -> None:
        if envelope.event_type == "hang":
            await asyncio.Event().wait()  # never returns
        seen.append(envelope.payload["n"])

    bus.subscribe("*", handler)
    await bus.relay.run_once()
    await bus.publish("d", "hang", {"n": "hang"})
    await bus.publish("d", "fine", {"n": "fine"})
    await _drain(bus.relay)

    assert seen == ["fine"]
    letters = await bus.outbox.dead_letters("stalls")
    assert [(letter.event.event_type, letter.error_type, letter.attempts) for letter in letters] == [
        ("hang", "TimeoutError", 2)
    ]


async def test_a_stop_that_cancels_a_delivery_gives_it_back(relational_backend: RelationalBackend) -> None:
    """A relay stopped past its shutdown timeout cancels the delivery in flight; the delivery is given back
    at once (unattempted), instead of waiting for its lease to end."""
    from pyfly.messaging.listener_container import ListenerContainerSettings

    engine = relational_backend.create_engine()
    settings = ListenerContainerSettings(shutdown_timeout=0.1)
    bus = await _bus(engine, group="slow", settings=settings, handler_timeout=None)
    started = asyncio.Event()

    async def slow(envelope: EventEnvelope) -> None:
        del envelope
        started.set()
        await asyncio.sleep(30)

    bus.subscribe("*", slow)
    await bus.start()
    try:
        await bus.publish("d", "e", {"n": 1})
        await asyncio.wait_for(started.wait(), timeout=10)
    finally:
        await bus.stop()

    pending = await bus.outbox.pending("slow")
    assert [(p.attempts, p.available_at <= bus.outbox.now()) for p in pending] == [(0, True)]


async def test_the_sql_dead_letter_store_keeps_entries_durably(relational_backend: RelationalBackend) -> None:
    """``SqlEdaDeadLetterStore``: the durable store the Kafka and RabbitMQ buses can record into."""
    from pyfly.eda.dlq import EdaDeadLetterEntry, SqlEdaDeadLetterStore

    engine = relational_backend.create_engine()
    store = SqlEdaDeadLetterStore(engine)
    await store.start()
    first = EdaDeadLetterEntry(
        event=EventEnvelope("order.created", {"id": 1, "note": "é"}, "orders", headers={"k": "v"}),
        error_type="ValueError",
        error_message="bad order",
        attempts=5,
    )
    second = EdaDeadLetterEntry(
        event=EventEnvelope("order.paid", {"id": 2}, "orders"), error_type="E", attempts=1, group="billing"
    )
    await store.add(first)
    await store.add(second)

    listed = await store.list(limit=10)
    assert {entry.id for entry in listed} == {first.id, second.id}
    stored = next(entry for entry in listed if entry.id == first.id)
    assert stored.event.payload == {"id": 1, "note": "é"}
    assert stored.event.headers == {"k": "v"}
    assert (stored.error_type, stored.error_message, stored.attempts) == ("ValueError", "bad order", 5)
    assert stored.event.timestamp.tzinfo is not None
    assert [entry.id for entry in await store.list(group="billing")] == [second.id]

    assert await store.delete(first.id) is True
    assert await store.delete(first.id) is False
    assert [entry.id for entry in await store.list()] == [second.id]
