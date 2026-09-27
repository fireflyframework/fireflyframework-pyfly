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
"""Real-backend integration tests for the EventPublisher buses.

Each broker bus subscribes a handler, starts, publishes an event, and asserts the handler receives the
correct envelope, against real Docker-backed brokers.

The PostgreSQL bus is driven through the constructor it always had (``dsn``, ``destinations``, ``group``,
``poll_interval_s``), on a fresh database per test, and pinned on what went wrong (WP09):

- C009/C010: rows that committed after a higher id was consumed were skipped forever;
- C064: a failing handler stalled its group and re-ran the healthy handlers on every retry;
- C145/C147: concurrent first publishes raced the DDL and leaked pools, LISTEN connections and consume loops,
  and a publish after ``stop()`` started everything again;
- C148: a lost LISTEN connection was never reopened, while the health indicator said UP.

Gated by ``@requires_docker``; collected only under ``-m integration``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.eda.health import EventPublisherHealthIndicator
from pyfly.eda.types import EventEnvelope
from pyfly.testing import requires_docker
from tests.support.backend_matrix import PG, RelationalBackend

OUTBOX_TABLE = "pyfly_outbox_events"


async def _wait_for(condition: Any, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
async def admin(relational_backend: RelationalBackend) -> AsyncIterator[AsyncEngine]:
    """An autocommit engine on the test's database, for the test's own inspection statements."""
    engine = create_async_engine(relational_backend.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        yield engine
    finally:
        await engine.dispose()


async def _listening(admin: AsyncEngine) -> int:
    """How many backends of the test's database last ran LISTEN."""
    async with admin.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND query LIKE 'LISTEN%'"
                    )
                )
            ).scalar_one()
        )


@pytest.mark.backends(PG)
async def test_postgres_event_bus_round_trip(relational_backend: RelationalBackend) -> None:
    """PostgresEventBus: publish→LISTEN/NOTIFY→handler round-trip."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = PostgresEventBus(dsn=relational_backend.url, destinations=["pyfly.events"], group="it")
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@pytest.mark.backends(PG)
async def test_a_publish_whose_commit_is_delayed_is_still_delivered(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """C010 (b): a trigger holds the first publish's transaction open after its id was taken; the second
    publish commits and is consumed; the cursor then stood past the first, which was never delivered."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    received: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope.event_type)

    consumer = PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="g1", poll_interval_s=0.2)
    producer = PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="producer")
    consumer.subscribe("*", handler)
    try:
        await consumer.start()  # subscribed first: the group is registered before anything is published
        async with admin.connect() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION wp09_slow() RETURNS trigger AS $$ BEGIN "
                    "IF NEW.event_type = 'slow' THEN PERFORM pg_sleep(1.5); END IF; RETURN NEW; END $$ "
                    "LANGUAGE plpgsql"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER wp09_slow BEFORE INSERT ON {OUTBOX_TABLE} "
                    "FOR EACH ROW EXECUTE FUNCTION wp09_slow()"
                )
            )
        slow = asyncio.create_task(producer.publish("d", "slow", {}))
        await asyncio.sleep(0.3)  # the slow row has its id and is still uncommitted
        await producer.publish("d", "fast", {})
        await _wait_for(lambda: "fast" in received)
        await slow
        await _wait_for(lambda: "slow" in received, timeout=10)
    finally:
        await producer.stop()
        await consumer.stop()
    assert sorted(received) == ["fast", "slow"]


@pytest.mark.backends(PG)
async def test_concurrent_publishers_lose_no_event(relational_backend: RelationalBackend) -> None:
    """C009 (c): 32 concurrent publishers on 4 buses; about 0.2 % of the events used to be lost."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    seen: list[int] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope.payload["n"])

    consumer = PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="g1", poll_interval_s=0.2)
    consumer.subscribe("*", handler)
    producers = [PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="p") for _ in range(4)]
    try:
        await consumer.start()

        async def publish(task: int) -> None:
            bus = producers[task % len(producers)]
            for index in range(40):
                await bus.publish("d", "e", {"n": task * 1000 + index})

        await asyncio.gather(*(publish(task) for task in range(32)))
        expected = sorted(task * 1000 + index for task in range(32) for index in range(40))
        await _wait_for(lambda: len(set(seen)) == len(expected), timeout=30)
    finally:
        for producer in producers:
            await producer.stop()
        await consumer.stop()
    assert sorted(set(seen)) == expected


@pytest.mark.backends(PG)
async def test_a_failing_handler_neither_stalls_the_group_nor_reruns_the_healthy_one(
    relational_backend: RelationalBackend,
) -> None:
    """C064: one poison event, then three normal ones; the group stalled on the poison event and the healthy
    handler saw it again on every retry."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    healthy: list[str] = []
    failing_attempts = 0

    async def healthy_handler(envelope: EventEnvelope) -> None:
        healthy.append(envelope.event_type)

    async def failing_handler(envelope: EventEnvelope) -> None:
        nonlocal failing_attempts
        failing_attempts += 1
        raise RuntimeError("cannot handle poison")

    bus = PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="g1", poll_interval_s=0.2)
    bus.subscribe("*", healthy_handler)
    bus.subscribe("poison", failing_handler)
    try:
        await bus.start()
        await bus.publish("d", "poison", {})
        for index in range(3):
            await bus.publish("d", f"after-{index}", {})
        await _wait_for(lambda: len(healthy) == 4)
        await asyncio.sleep(1.5)  # the poison event's second attempt is due after its back-off
    finally:
        await bus.stop()
    assert healthy == ["poison", "after-0", "after-1", "after-2"]
    assert failing_attempts >= 2  # attempted again, alone


@pytest.mark.backends(PG)
async def test_concurrent_first_publishes_start_nothing_and_race_nothing(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """C145/C147: five concurrent first publishes on a bus nobody started, against an empty database."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    creator = PostgresEventBus(dsn=relational_backend.url)
    await creator.start()  # the tables exist (a first boot created them); the publisher never starts
    await creator.stop()

    bus = PostgresEventBus(dsn=relational_backend.url, destinations=["d"])
    try:
        outcomes = await asyncio.gather(*(bus.publish("d", "e", {"n": n}) for n in range(5)), return_exceptions=True)
        assert outcomes == [None] * 5
        assert await _listening(admin) == 0  # a publish starts no LISTEN connection and no consume loop
    finally:
        await bus.stop()

    await bus.publish("d", "after-stop", {})  # still written, nothing restarted
    assert await _listening(admin) == 0
    await bus.stop()
    async with admin.connect() as conn:
        assert (await conn.execute(text(f"SELECT count(*) FROM {OUTBOX_TABLE}"))).scalar_one() == 6


@pytest.mark.backends(PG)
async def test_concurrent_first_boots_all_succeed_and_a_failed_start_leaves_nothing(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """C147: four concurrent first boots against an empty database: three failed with a duplicate pg_type
    key. And a start whose LISTEN connection is refused left its pool behind."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    buses = [PostgresEventBus(dsn=relational_backend.url, group=f"g{n}") for n in range(4)]
    try:
        outcomes = await asyncio.gather(*(bus.start() for bus in buses), return_exceptions=True)
        assert outcomes == [None] * 4
        assert await _listening(admin) == 4
    finally:
        for bus in buses:
            await bus.stop()
    assert await _listening(admin) == 0

    wrong = make_url(relational_backend.url).set(username="nobody", password="wrong")
    refused = PostgresEventBus(
        dsn=relational_backend.url, listen_dsn=wrong.render_as_string(hide_password=False), group="refused"
    )
    with pytest.raises(Exception, match="(?i)password|auth|role"):
        await refused.start()
    assert refused.running is False
    assert refused.relay.running is False
    await refused.stop()


@pytest.mark.backends(PG)
async def test_concurrent_starts_open_one_listen_connection(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    from pyfly.eda.adapters.postgres import PostgresEventBus

    bus = PostgresEventBus(dsn=relational_backend.url, group="once")
    try:
        await asyncio.gather(*(bus.start() for _ in range(5)))
        assert await _listening(admin) == 1
        await bus.start()
        assert await _listening(admin) == 1
    finally:
        await bus.stop()
        await bus.stop()  # idempotent
    assert await _listening(admin) == 0


@pytest.mark.backends(PG)
async def test_a_lost_listen_connection_is_reopened_and_reported_meanwhile(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """C148: after pg_terminate_backend of the LISTEN connection, every event waited for the poll, for the
    rest of the process, and health said UP."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    arrived: dict[str, float] = {}

    async def handler(envelope: EventEnvelope) -> None:
        arrived[envelope.event_type] = time.monotonic()

    bus = PostgresEventBus(dsn=relational_backend.url, destinations=["d"], group="g1", poll_interval_s=1.0)
    bus.subscribe("*", handler)
    health = EventPublisherHealthIndicator(bus)
    try:
        await bus.start()
        sent = time.monotonic()
        await bus.publish("d", "before", {})
        await _wait_for(lambda: "before" in arrived)
        assert arrived["before"] - sent < 0.5  # woken by NOTIFY
        assert (await health.health()).status == "UP"

        async with admin.connect() as conn:
            killed = (
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() AND query LIKE 'LISTEN%'"
                    )
                )
            ).all()
        assert killed == [(True,)]
        await _wait_for(lambda: bus.listener_state.value == "reconnecting", timeout=5)
        status = await health.health()
        assert status.status == "DOWN"
        assert status.details["listener"] == "reconnecting"

        await _wait_for(lambda: bus.listener_state.value == "listening", timeout=10)
        assert await _listening(admin) == 1
        assert (await health.health()).status == "UP"

        # Wake-ups are back: well under the one-second poll.
        await asyncio.sleep(1.2)  # let the relay settle into a full poll wait
        sent = time.monotonic()
        await bus.publish("d", "after", {})
        await _wait_for(lambda: "after" in arrived)
        assert arrived["after"] - sent < 0.5
    finally:
        await bus.stop()


@requires_docker
@pytest.mark.asyncio
async def test_redis_event_bus_round_trip(redis_url: str) -> None:
    """RedisStreamsEventBus: publish→XREADGROUP→handler round-trip."""
    from pyfly.eda.adapters.redis import RedisStreamsEventBus

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = RedisStreamsEventBus(
        url=redis_url,
        streams=["pyfly.events"],
        group="it",
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        # Group is created at "$" so publish AFTER start to avoid missing the entry.
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@requires_docker
@pytest.mark.asyncio
async def test_kafka_event_bus_round_trip(kafka_url: str) -> None:
    """KafkaEventBus: publish→consume→handler round-trip on a unique topic."""
    from pyfly.eda.adapters.kafka import KafkaEventBus

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = KafkaEventBus(
        bootstrap_servers=kafka_url,
        topics=[topic],
        group=group,
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish(topic, "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_event_bus_round_trip(amqp_url: str) -> None:
    """RabbitMqEventBus: publish→queue→handler round-trip on a unique destination."""
    from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus

    destination = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = RabbitMqEventBus(
        url=amqp_url,
        destinations=[destination],
        group=group,
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish(destination, "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@pytest.mark.backends(PG)
async def test_the_auto_configured_bus_runs_on_the_primary_datasource(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """provider=postgres needs no DSN of its own: the bus is on the registry's primary datasource, and a
    ``pyfly.eda.postgres.dsn`` equal to the primary URL reuses it (no second connection pool)."""
    from pyfly.container.stereotypes import service
    from pyfly.context.application_context import ApplicationContext
    from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.eda.adapters.postgres import PostgresEventBus
    from pyfly.eda.auto_configuration import EdaAutoConfiguration
    from pyfly.eda.decorators import event_listener
    from pyfly.eda.ports.outbound import EventPublisher

    @service
    class OrderEvents:
        def __init__(self) -> None:
            self.seen: list[str] = []

        @event_listener(["order.*"])
        async def on_order(self, envelope: EventEnvelope) -> None:
            self.seen.append(envelope.event_type)

    for overrides in ({}, {"pyfly.eda.postgres.dsn": relational_backend.url}):
        config = relational_backend.config({"pyfly.eda.provider": "postgres", "pyfly.eda.group": "orders", **overrides})
        ctx = ApplicationContext(config)
        for bean in (RelationalAutoConfiguration, EdaAutoConfiguration, OrderEvents):
            ctx.register_bean(bean)
        await ctx.start()
        try:
            bus = ctx.get_bean(EventPublisher)
            assert isinstance(bus, PostgresEventBus)
            registry = ctx.get_bean(DataSourceRegistry)
            assert bus.outbox.datasource is registry.primary
            assert registry.names() == ["primary"]
            await bus.publish("pyfly.events", "order.created", {"id": 1})
            seen = ctx.get_bean(OrderEvents).seen
            await _wait_for(lambda seen=seen: seen == ["order.created"])
        finally:
            await ctx.stop()
        assert await _listening(admin) == 0
