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
"""The transactional outbox as a layer over any transport (WP09b), on every relational lane.

``TransactionalEventPublisher`` makes a transport's publish part of the caller's unit of work: the event is
appended to the outbox store, owed to the forwarding relay's group, and ``OutboxForwarder`` publishes it to the
transport once the unit committed, at least once. The transport here is the in-process bus (its subscribers stand
for a broker's consumers); the Kafka and RabbitMQ lanes are ``test_outbox_forwarding_brokers.py``. SQLite file
(foreign keys on), PostgreSQL, MySQL 8 and MariaDB 11.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, is_transaction_active
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox import SqlOutboxStore
from pyfly.eda.outbox_forwarding import (
    FORWARD_GROUP_PREFIX,
    OutboxForwarder,
    PublisherState,
    TransactionalEventPublisher,
)
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.listener_container import FixedBackOff, RetryPolicy
from tests.support.backend_matrix import RelationalBackend


class ForwardedOrder(Base):
    __tablename__ = "wp09b_forwarded_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class Consumer:
    """A subscriber of the in-process transport: what a broker's consumer would receive."""

    def __init__(self, *, fail: int = 0) -> None:
        self.received: list[EventEnvelope] = []
        self.fail = fail
        self.in_transaction: list[bool] = []

    async def __call__(self, envelope: EventEnvelope) -> None:
        self.in_transaction.append(is_transaction_active())
        if self.fail:
            self.fail -= 1
            raise ConnectionError("the broker is unreachable")
        self.received.append(envelope)

    def numbers(self) -> list[Any]:
        return [envelope.payload["n"] for envelope in self.received]


async def _publisher(
    engine: AsyncEngine, transport: EventPublisher | None = None, **options: Any
) -> tuple[TransactionalEventPublisher, InMemoryEventBus, Consumer]:
    bus = transport if isinstance(transport, InMemoryEventBus) else InMemoryEventBus()
    consumer = Consumer()
    bus.subscribe("*", consumer)
    options.setdefault("retry", RetryPolicy(max_attempts=3, backoff=FixedBackOff(0.0)))
    options.setdefault("name", "memory")
    publisher = TransactionalEventPublisher(bus, SqlOutboxStore(engine), **options)
    await publisher.store.start()
    return publisher, bus, consumer


async def _drain(publisher: TransactionalEventPublisher) -> None:
    for _ in range(100):
        if await publisher.relay.run_once() == 0:
            return
    raise AssertionError("the forwarder never went quiet")


async def _orders(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(ForwardedOrder))).scalar_one())


async def test_a_unit_that_rolls_back_forwards_nothing_and_one_that_commits_forwards_once(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    await relational_backend.create_tables(ForwardedOrder)
    publisher, _bus, consumer = await _publisher(engine)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError, match="payment declined"):
        async with template.transaction() as unit:
            assert unit is not None
            unit.resource.add(ForwardedOrder(name="order-1"))
            await publisher.publish("orders", "order.placed", {"n": 1})
            raise RuntimeError("payment declined")
    await _drain(publisher)
    assert consumer.received == []
    assert await publisher.pending() == []

    async with template.transaction() as unit:
        assert unit is not None
        unit.resource.add(ForwardedOrder(name="order-2"))
        await publisher.publish("orders", "order.placed", {"n": 2}, {"x-trace": "t-2"})
        assert consumer.received == []  # nothing reaches the transport before the commit
    await _drain(publisher)

    assert await _orders(engine) == 1
    assert consumer.numbers() == [2]
    (envelope,) = consumer.received
    assert (envelope.destination, envelope.event_type) == ("orders", "order.placed")
    assert envelope.headers["x-trace"] == "t-2"
    assert envelope.headers[EVENT_ID_HEADER]  # the outbox event's id: the same on every attempt
    assert await publisher.pending() == []
    assert publisher.relay.counters.delivered == 1


async def test_a_publish_outside_a_unit_is_durable_in_a_short_unit_of_its_own(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    publisher, _bus, consumer = await _publisher(engine)

    await publisher.publish("orders", "order.placed", {"n": 1})

    assert [p.envelope.payload for p in await publisher.pending()] == [{"n": 1}]
    assert consumer.received == []
    await _drain(publisher)
    assert consumer.numbers() == [1]


async def test_a_republished_event_carries_the_same_event_id_header(relational_backend: RelationalBackend) -> None:
    """At least once: a failed publish is attempted again, and every copy the transport gets carries the event's
    id, so a consumer deduplicates on ``x-pyfly-event-id``. A header the publisher set (a domain event's id) is
    kept."""
    engine = relational_backend.create_engine()
    publisher, bus, consumer = await _publisher(engine)
    flaky = Consumer(fail=1)
    bus.subscribe("*", flaky)

    await publisher.publish("orders", "order.placed", {"n": 1})
    await publisher.publish("orders", "order.placed", {"n": 2}, {EVENT_ID_HEADER: "domain-event-2"})
    await _drain(publisher)

    ids = [envelope.headers[EVENT_ID_HEADER] for envelope in consumer.received]
    # The first was forwarded again after the other subscriber failed it, behind the second: a retry reorders.
    assert consumer.numbers() == [1, 2, 1]
    assert ids[0] == ids[2] != "domain-event-2"
    assert ids[1] == "domain-event-2"
    assert flaky.numbers() == [2, 1]


async def test_the_forwarder_publishes_outside_the_unit_of_the_code_that_runs_it(
    relational_backend: RelationalBackend,
) -> None:
    """The transport's publish never joins a unit of work: run in a unit (a data test's own relay round), the
    subscriber sees no transaction."""
    engine = relational_backend.create_engine()
    publisher, _bus, consumer = await _publisher(engine)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    await publisher.publish("orders", "order.placed", {"n": 1})

    async with template.transaction():
        await _drain(publisher)

    assert consumer.numbers() == [1]
    assert consumer.in_transaction == [False]


async def test_a_failing_transport_is_retried_then_dead_lettered_and_the_others_go_on(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    bus = InMemoryEventBus()
    unreachable = Consumer(fail=10)
    bus.subscribe("payment.*", unreachable)
    publisher, _bus, consumer = await _publisher(engine, bus)  # three attempts, no back-off

    await publisher.publish("payments", "payment.taken", {"n": "poison"})
    await publisher.publish("orders", "order.placed", {"n": 1})
    await _drain(publisher)

    assert unreachable.fail == 7  # three attempts
    assert consumer.numbers() == [1]  # the other event went on
    (letter,) = await publisher.dead_letters()
    assert (letter.event.event_type, letter.error_type, letter.attempts) == ("payment.taken", "ConnectionError", 3)
    assert letter.group == publisher.group
    assert publisher.relay.counters.dead_lettered == 1
    assert await publisher.pending() == []


async def test_events_other_writers_append_for_the_forwarded_destinations_are_forwarded(
    relational_backend: RelationalBackend,
) -> None:
    """The forwarder's group is registered for its destinations: an event another writer of the store appends
    to one of them (a process that writes the outbox directly) is forwarded too, and one to another destination
    is not."""
    engine = relational_backend.create_engine()
    publisher, _bus, consumer = await _publisher(engine, destinations=["orders"])
    await publisher.relay.register()
    writer = SqlOutboxStore(engine)

    await writer.append(EventEnvelope("order.placed", {"n": "orders"}, "orders"))
    await writer.append(EventEnvelope("login", {"n": "audit"}, "audit"))
    await _drain(publisher)

    assert consumer.numbers() == ["orders"]


async def test_a_destination_the_forwarder_does_not_take_goes_straight_to_the_transport_after_the_commit(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    publisher, _bus, consumer = await _publisher(engine, destinations=["orders"])
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError):
        async with template.transaction():
            await publisher.publish("metrics", "tick", {"n": "rolled back"})
            raise RuntimeError
    async with template.transaction():
        await publisher.publish("metrics", "tick", {"n": "committed"})
        assert consumer.received == []
    assert consumer.numbers() == ["committed"]  # at the commit, not through the outbox
    await publisher.publish("metrics", "tick", {"n": "outside"})  # outside a unit: at once
    assert consumer.numbers() == ["committed", "outside"]
    assert await publisher.pending() == []


async def test_the_publisher_starts_and_stops_its_transport_store_and_forwarder(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    publisher, _bus, consumer = await _publisher(engine, poll_interval=0.05)
    assert (publisher.phase, publisher.joins_transactions) == (CONSUMER_PHASE, True)
    assert publisher.group == f"{FORWARD_GROUP_PREFIX}memory"
    assert isinstance(publisher, EventPublisher)
    assert isinstance(publisher.forwarder, OutboxForwarder) and publisher.relay is publisher.forwarder
    down = await publisher.health_status()
    assert (down.status, down.details["reason"]) == ("DOWN", "publisher new")

    await publisher.start()
    await publisher.start()
    try:
        assert publisher.state is PublisherState.RUNNING
        assert publisher.forwarder.alive
        template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
        async with template.transaction():
            await publisher.publish("orders", "order.placed", {"n": 1})
        for _ in range(500):  # the commit wakes the forwarder
            if publisher.relay.counters.delivered:
                break
            await asyncio.sleep(0.01)
        assert consumer.numbers() == [1]
        health = await publisher.health_status()
        assert health.status == "UP", health.details
        assert (health.details["transport"], health.details["forwarded"]) == ("InMemoryEventBus", 1)
    finally:
        await publisher.stop()
        await publisher.stop()
    assert publisher.state is PublisherState.STOPPED
    assert not publisher.forwarder.alive
    assert (await publisher.health_status()).status == "DOWN"

    await publisher.publish("orders", "order.placed", {"n": 2})  # after stop: still durable, for a later relay
    assert [p.envelope.payload["n"] for p in await publisher.pending()] == [2]


async def test_a_forwarder_task_that_ended_under_a_running_publisher_reports_down(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    publisher, _bus, _consumer = await _publisher(engine)
    await publisher.start()
    try:
        task = publisher.forwarder._task
        assert task is not None
        task.cancel()
        await asyncio.wait({task})
        health = await publisher.health_status()
        assert (health.status, health.details["reason"]) == ("DOWN", "the forwarder's task ended")
    finally:
        await publisher.stop()
