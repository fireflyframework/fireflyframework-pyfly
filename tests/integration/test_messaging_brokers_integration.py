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
"""Kafka and RabbitMQ against real brokers, with PostgreSQL as the application database.

Round trips (the adapters alone):

- publish→subscribe with the subscription made BEFORE ``start()``, and with it made after.

Delivery guarantees (a real ``ApplicationContext``: ``@message_listener`` or ``@event_listener`` plus
``@transactional`` and a repository write, the listener container running every delivery in a unit of
work of its own and acknowledging it only after that unit committed):

- C012/C008/C014: a listener that fails once is delivered again, and its row is committed once; on Kafka
  the group's committed offset reaches the end of the log only after that;
- C013: ``ctx.stop()`` while a handler is inside its transaction waits for it (the row and the offset are
  committed, nothing is delivered again after a restart), and a handler still running at the listener's
  shutdown timeout is cancelled without its offset or ack, so the restarted application gets it again;
- C015: a backlog of 1000 RabbitMQ messages with a two-connection pool runs at most two handlers at once
  and loses nothing;
- C065 and the dead-letter paths: a listener that keeps failing is attempted ``max-attempts`` times with a
  delay, then dead-lettered (``<topic>.DLT`` on Kafka, ``<queue>.dlq`` behind ``<exchange>.dlx`` on
  RabbitMQ), and acknowledged;
- the delivery's unit takes the listener's ``@transactional`` settings: PostgreSQL runs a ``SERIALIZABLE``
  read-only listener at ``SERIALIZABLE``, read-only, and a ``REQUIRES_NEW`` listener runs in its own unit
  only, so a backlog on a two-connection pool needs one connection per delivery.

Gated by ``@requires_docker``; collected only under ``-m integration``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container import bean, configuration
from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import Isolation, Propagation, current_unit_of_work
from pyfly.data.transactional import transactional
from pyfly.eda.decorators import event_listener
from pyfly.eda.dlq import EdaDeadLetterStore, InMemoryEdaDeadLetterStore
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.messaging.decorators import message_listener
from pyfly.messaging.listener_container import is_transient_failure
from pyfly.messaging.types import Message
from pyfly.testing import requires_docker
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend

# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_kafka_adapter_publish_subscribe_round_trip(kafka_url: str) -> None:
    """KafkaAdapter: subscribe before start → publish → handler receives message."""
    from pyfly.messaging.adapters.kafka import KafkaAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = KafkaAdapter(bootstrap_servers=kafka_url)
    await adapter.subscribe(topic, handler, group=group)
    try:
        await adapter.start()
        await adapter.publish(topic, b'{"id": 1}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 1}'
    assert received[0].topic == topic


@requires_docker
@pytest.mark.asyncio
async def test_kafka_adapter_subscribe_after_start(kafka_url: str) -> None:
    """KafkaAdapter: start FIRST, subscribe after → publish → handler receives message."""
    from pyfly.messaging.adapters.kafka import KafkaAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = KafkaAdapter(bootstrap_servers=kafka_url)
    try:
        await adapter.start()
        # Subscribe after start — must spin up a consumer immediately.
        await adapter.subscribe(topic, handler, group=group)
        await adapter.publish(topic, b'{"id": 2}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 2}'
    assert received[0].topic == topic


# ---------------------------------------------------------------------------
# RabbitMQ
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_adapter_publish_subscribe_round_trip(amqp_url: str) -> None:
    """RabbitMQAdapter: subscribe before start → publish → handler receives message."""
    from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it-q-{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = RabbitMQAdapter(url=amqp_url)
    await adapter.subscribe(topic, handler, group=group)
    try:
        await adapter.start()
        await adapter.publish(topic, b'{"id": 1}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 1}'
    assert received[0].topic == topic


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_adapter_subscribe_after_start(amqp_url: str) -> None:
    """RabbitMQAdapter: start FIRST, subscribe after → publish → handler receives message."""
    from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it-q-{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = RabbitMQAdapter(url=amqp_url)
    try:
        await adapter.start()
        # Subscribe after start — must bind its own queue immediately.
        await adapter.subscribe(topic, handler, group=group)
        await adapter.publish(topic, b'{"id": 2}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 2}'
    assert received[0].topic == topic


# ---------------------------------------------------------------------------
# Delivery guarantees: a real ApplicationContext, a @transactional listener, PostgreSQL
# ---------------------------------------------------------------------------


class BrokerOrder(Base):
    __tablename__ = "wp12_broker_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    body: Mapped[str] = mapped_column(String(64))


@repository
class BrokerOrderRepository(Repository[BrokerOrder, int]):
    pass


WARMUP = "warmup"
"""Bodies starting with this prove the listener consumes; the handler writes nothing for them."""


@dataclass
class Scenario:
    """What a listener does with each body, and what it saw."""

    fail_first: set[str] = field(default_factory=set)
    """Bodies whose first attempt writes its row and then fails (the unit must roll it back)."""
    always_fail: set[str] = field(default_factory=set)
    """Bodies that fail on every attempt."""
    hold: dict[str, float] = field(default_factory=dict)
    """Bodies whose handler stays inside its transaction, after writing, for that many seconds."""
    work: float = 0.0
    """Seconds every handler spends inside its transaction after writing."""
    attempts: Counter[str] = field(default_factory=Counter)
    started: set[str] = field(default_factory=set)
    warmed_up: asyncio.Event = field(default_factory=asyncio.Event)
    running: int = 0
    peak: int = 0

    async def handle(self, repo: BrokerOrderRepository, body: str) -> None:
        if body.startswith(WARMUP):
            self.warmed_up.set()
            return
        self.attempts[body] += 1
        self.started.add(body)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await repo.save(BrokerOrder(body=body))
            if self.work:
                await asyncio.sleep(self.work)
            if body in self.hold:
                await asyncio.sleep(self.hold[body])
            if body in self.always_fail or (body in self.fail_first and self.attempts[body] == 1):
                raise RuntimeError(f"listener fails on {body} (attempt {self.attempts[body]})")
        finally:
            self.running -= 1


def _names(prefix: str) -> tuple[str, str]:
    token = uuid.uuid4().hex[:8]
    return f"wp12.{prefix}.{token}", f"wp12-{prefix}-{token}"


def _message_listener(topic: str, group: str, scenario: Scenario) -> type:
    @service
    class OrderMessageListener:
        def __init__(self, repo: BrokerOrderRepository) -> None:
            self.repo = repo

        @message_listener(topic, group=group)
        @transactional
        async def on_order(self, message: Message) -> None:
            await scenario.handle(self.repo, message.value.decode())

    return OrderMessageListener


def _event_listener(scenario: Scenario) -> type:
    @service
    class OrderEventListener:
        def __init__(self, repo: BrokerOrderRepository) -> None:
            self.repo = repo

        @event_listener(["order.*"])
        @transactional
        async def on_order(self, envelope: EventEnvelope) -> None:
            await scenario.handle(self.repo, str(envelope.payload["body"]))

    return OrderEventListener


@configuration
class DeadLetterStoreConfig:
    @bean
    def eda_dead_letter_store(self) -> EdaDeadLetterStore:
        return InMemoryEdaDeadLetterStore()


FAST_RETRY = {
    "retry.initial-delay": "0.05",
    "retry.multiplier": "1.0",
    "retry.max-attempts": "3",
}


def _listener_settings(prefix: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    return {f"{prefix}.listener.{key}": value for key, value in {**FAST_RETRY, **(overrides or {})}.items()}


def _kafka_messaging(kafka_url: str, **listener: str) -> dict[str, str]:
    return {
        "pyfly.messaging.provider": "kafka",
        "pyfly.messaging.kafka.bootstrap-servers": kafka_url,
        "pyfly.messaging.kafka.auto-offset-reset": "earliest",
        **_listener_settings("pyfly.messaging", listener),
    }


def _rabbit_messaging(amqp_url: str, **listener: str) -> dict[str, str]:
    return {
        "pyfly.messaging.provider": "rabbitmq",
        "pyfly.messaging.rabbitmq.url": amqp_url,
        **_listener_settings("pyfly.messaging", listener),
    }


def _kafka_eda(kafka_url: str, topic: str, group: str, **listener: str) -> dict[str, str]:
    return {
        "pyfly.messaging.provider": "memory",
        "pyfly.eda.provider": "kafka",
        "pyfly.eda.kafka.bootstrap-servers": kafka_url,
        "pyfly.eda.destinations": topic,
        "pyfly.eda.group": group,
        **_listener_settings("pyfly.eda", listener),
    }


def _rabbit_eda(amqp_url: str, destination: str, group: str, **listener: str) -> dict[str, str]:
    return {
        "pyfly.messaging.provider": "memory",
        "pyfly.eda.provider": "rabbitmq",
        "pyfly.eda.rabbitmq.url": amqp_url,
        "pyfly.eda.destinations": destination,
        "pyfly.eda.group": group,
        **_listener_settings("pyfly.eda", listener),
    }


async def _boot(
    backend: RelationalBackend, overrides: dict[str, str], listener: type, *beans: type
) -> ApplicationContext:
    ctx = ApplicationContext(backend.config(overrides))
    for registered in (RelationalAutoConfiguration, BrokerOrderRepository, listener, *beans):
        ctx.register_bean(registered)
    await ctx.start()
    return ctx


async def _bodies(backend: RelationalBackend) -> list[str]:
    """The committed rows, as another process sees them."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return sorted(row[0] for row in (await conn.execute(text("SELECT body FROM wp12_broker_order"))).all())
    finally:
        await engine.dispose()


async def _eventually(check: Callable[[], Awaitable[bool]], *, timeout: float = 30.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what or check}")
        await asyncio.sleep(0.1)


async def _settled(check: Callable[[], Awaitable[bool]], *, hold: float = 2.0) -> bool:
    """Whether *check* stays true for *hold* seconds (nothing more arrives)."""
    deadline = time.monotonic() + hold
    while time.monotonic() < deadline:
        if not await check():
            return False
        await asyncio.sleep(0.1)
    return True


async def _warm_up(scenario: Scenario, send: Callable[[str], Awaitable[None]]) -> None:
    """Send warm-up messages until the listener consumes one: its consumer has joined and is fetching."""
    deadline = time.monotonic() + 45
    attempt = 0
    while not scenario.warmed_up.is_set():
        if time.monotonic() > deadline:
            raise AssertionError("the listener never consumed a warm-up message")
        attempt += 1
        await send(f"{WARMUP}-{attempt}")
        try:
            await asyncio.wait_for(scenario.warmed_up.wait(), timeout=1.0)
        except TimeoutError:
            continue


# -- Kafka helpers ------------------------------------------------------------------------------------------


def _kafka_sender(kafka_url: str, topic: str) -> Callable[[str], Awaitable[None]]:
    async def send(body: str) -> None:
        from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

        producer = AIOKafkaProducer(bootstrap_servers=kafka_url)
        await producer.start()
        try:
            await producer.send_and_wait(topic, body.encode())
        finally:
            await producer.stop()

    return send


def _event_sender(ctx: ApplicationContext, topic: str) -> Callable[[str], Awaitable[None]]:
    async def send(body: str) -> None:
        bus = ctx.get_bean(EventPublisher)  # type: ignore[type-abstract]
        await bus.publish(topic, "order.created", {"body": body})

    return send


async def _kafka_committed(kafka_url: str, group: str, topic: str) -> int | None:
    """The group's committed offset on partition 0 of *topic* (``None`` when it committed none)."""
    from aiokafka.admin import AIOKafkaAdminClient  # type: ignore[import-untyped]

    admin = AIOKafkaAdminClient(bootstrap_servers=kafka_url)
    await admin.start()
    try:
        offsets = await admin.list_consumer_group_offsets(group)
    finally:
        await admin.close()
    for tp, meta in offsets.items():
        if tp.topic == topic and tp.partition == 0:
            return int(meta.offset) if meta.offset >= 0 else None
    return None


async def _kafka_log_end(kafka_url: str, topic: str) -> int:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    consumer = AIOKafkaConsumer(bootstrap_servers=kafka_url)
    await consumer.start()
    try:
        tp = TopicPartition(topic, 0)
        ends = await consumer.end_offsets([tp])
        return int(ends[tp])
    finally:
        await consumer.stop()


async def _kafka_records(kafka_url: str, topic: str, *, expected: int, timeout: float = 20.0) -> list[Any]:
    """The first *expected* records of *topic*, read from the beginning without a group."""
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(topic, bootstrap_servers=kafka_url, auto_offset_reset="earliest")
    await consumer.start()
    records: list[Any] = []
    try:
        deadline = time.monotonic() + timeout
        while len(records) < expected and time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for tp_records in batch.values():
                records.extend(tp_records)
    finally:
        await consumer.stop()
    return records


# -- RabbitMQ helpers ---------------------------------------------------------------------------------------


def _rabbit_sender(amqp_url: str, routing_key: str) -> Callable[[str], Awaitable[None]]:
    async def send(body: str) -> None:
        import aio_pika

        connection = await aio_pika.connect_robust(amqp_url)
        try:
            channel = await connection.channel()
            exchange = await channel.declare_exchange("pyfly", aio_pika.ExchangeType.DIRECT, durable=True)
            await exchange.publish(
                aio_pika.Message(body=body.encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=routing_key,
            )
        finally:
            await connection.close()

    return send


async def _rabbit_backlog(amqp_url: str, queue: str, routing_key: str, bodies: list[str]) -> None:
    """Declare *queue* the way the adapter does and fill it before the application starts."""
    import aio_pika

    connection = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange("pyfly", aio_pika.ExchangeType.DIRECT, durable=True)
        declared = await channel.declare_queue(queue, durable=True)
        await declared.bind(exchange, routing_key=routing_key)
        for body in bodies:
            await exchange.publish(
                aio_pika.Message(body=body.encode(), delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=routing_key,
            )
    finally:
        await connection.close()


async def _rabbit_queue(amqp_url: str, queue: str) -> list[Any]:
    """Every message waiting in *queue* (consumed with an ack, so the queue ends empty)."""
    import aio_pika

    connection = await aio_pika.connect_robust(amqp_url)
    messages: list[Any] = []
    try:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue, durable=True, passive=True)
        while True:
            message = await declared.get(no_ack=True, fail=False)
            if message is None:
                return messages
            messages.append(message)
    finally:
        await connection.close()


async def _rabbit_depth(amqp_url: str, queue: str) -> int:
    import aio_pika

    connection = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await connection.channel()
        declared = await channel.declare_queue(queue, durable=True, passive=True)
        return int(declared.declaration_result.message_count or 0)
    finally:
        await connection.close()


async def _rabbit_delete(amqp_url: str, *queues: str) -> None:
    import aio_pika

    connection = await aio_pika.connect_robust(amqp_url)
    try:
        channel = await connection.channel()
        for queue in queues:
            try:
                await channel.queue_delete(queue)
            except Exception:  # noqa: BLE001 — a queue the test never created
                channel = await connection.channel()
    finally:
        await connection.close()


# -- C012: Kafka @message_listener -------------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_kafka_listener_that_fails_once_is_delivered_again_and_committed_once(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("kafka-retry")
    scenario = Scenario(fail_first={"m2"})
    send = _kafka_sender(kafka_url, topic)
    ctx = await _boot(relational_backend, _kafka_messaging(kafka_url), _message_listener(topic, group, scenario))
    try:
        await _warm_up(scenario, send)
        for body in ("m1", "m2", "m3"):
            await send(body)

        async def all_rows() -> bool:
            return await _bodies(relational_backend) == ["m1", "m2", "m3"]

        await _eventually(all_rows, what="m1, m2 and m3 committed")
        log_end = await _kafka_log_end(kafka_url, topic)

        async def offset_at_end() -> bool:
            return await _kafka_committed(kafka_url, group, topic) == log_end

        await _eventually(offset_at_end, what="the committed offset at the end of the log")
    finally:
        await ctx.stop()

    assert scenario.attempts["m2"] == 2
    assert scenario.attempts["m1"] == scenario.attempts["m3"] == 1
    assert await _bodies(relational_backend) == ["m1", "m2", "m3"]


# -- C013: Kafka graceful stop -----------------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_kafka_stop_waits_for_the_handler_in_flight_and_commits_its_offset(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("kafka-drain")
    first = Scenario(hold={"slow": 1.5})
    send = _kafka_sender(kafka_url, topic)
    ctx = await _boot(relational_backend, _kafka_messaging(kafka_url), _message_listener(topic, group, first))
    try:
        await _warm_up(first, send)
        await send("slow")

        async def in_flight() -> bool:
            return "slow" in first.started

        await _eventually(in_flight, what="the slow handler to start")
    finally:
        await ctx.stop()  # a rolling deploy: SIGTERM while the handler is inside its transaction

    assert await _bodies(relational_backend) == ["slow"]
    assert await _kafka_committed(kafka_url, group, topic) == await _kafka_log_end(kafka_url, topic)

    second = Scenario()
    ctx = await _boot(relational_backend, _kafka_messaging(kafka_url), _message_listener(topic, group, second))
    try:
        await _warm_up(second, send)

        async def unchanged() -> bool:
            return await _bodies(relational_backend) == ["slow"]

        assert await _settled(unchanged)
    finally:
        await ctx.stop()
    assert second.attempts["slow"] == 0


@requires_docker
@pytest.mark.backends(PG)
async def test_kafka_handler_cancelled_at_the_shutdown_timeout_is_delivered_after_a_restart(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("kafka-cancel")
    first = Scenario(hold={"stuck": 60.0})
    send = _kafka_sender(kafka_url, topic)
    config = _kafka_messaging(kafka_url, **{"shutdown-timeout": "0.5"})
    ctx = await _boot(relational_backend, config, _message_listener(topic, group, first))
    try:
        await _warm_up(first, send)
        await send("stuck")

        async def in_flight() -> bool:
            return "stuck" in first.started

        await _eventually(in_flight, what="the stuck handler to start")
    finally:
        await ctx.stop()

    assert await _bodies(relational_backend) == []  # cancelled inside its transaction: rolled back

    second = Scenario()
    ctx = await _boot(relational_backend, config, _message_listener(topic, group, second))
    try:

        async def redelivered() -> bool:
            return await _bodies(relational_backend) == ["stuck"]

        await _eventually(redelivered, what="the cancelled record delivered again after the restart")
    finally:
        await ctx.stop()
    assert second.attempts["stuck"] == 1


# -- Kafka dead-letter topic -------------------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_kafka_listener_that_keeps_failing_goes_to_the_dead_letter_topic_after_max_attempts(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("kafka-dlt")
    scenario = Scenario(always_fail={"poison"})
    send = _kafka_sender(kafka_url, topic)
    ctx = await _boot(relational_backend, _kafka_messaging(kafka_url), _message_listener(topic, group, scenario))
    try:
        await _warm_up(scenario, send)
        await send("poison")
        await send("after")

        async def after_committed() -> bool:
            return await _bodies(relational_backend) == ["after"]

        await _eventually(after_committed, what="the record after the poison one")
        dead = await _kafka_records(kafka_url, f"{topic}.DLT", expected=1)
        log_end = await _kafka_log_end(kafka_url, topic)

        async def offset_at_end() -> bool:
            return await _kafka_committed(kafka_url, group, topic) == log_end

        await _eventually(offset_at_end, what="the committed offset past the dead-lettered record")
    finally:
        await ctx.stop()

    assert scenario.attempts["poison"] == 3
    assert [record.value for record in dead] == [b"poison"]
    headers = {key: value.decode() for key, value in dead[0].headers}
    assert headers["x-original-topic"] == topic
    assert headers["x-exception"] == "RuntimeError"
    assert headers["x-dlt-attempts"] == "3"


# -- C008: EDA KafkaEventBus -------------------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_eda_kafka_event_listener_that_fails_once_is_delivered_again_and_committed_once(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("eda-kafka")
    scenario = Scenario(fail_first={"e2"})
    ctx = await _boot(relational_backend, _kafka_eda(kafka_url, topic, group), _event_listener(scenario))
    send = _event_sender(ctx, topic)
    try:
        await _warm_up(scenario, send)
        for body in ("e1", "e2", "e3"):
            await send(body)

        async def all_rows() -> bool:
            return await _bodies(relational_backend) == ["e1", "e2", "e3"]

        await _eventually(all_rows, what="e1, e2 and e3 committed")
        log_end = await _kafka_log_end(kafka_url, topic)

        async def offset_at_end() -> bool:
            return await _kafka_committed(kafka_url, group, topic) == log_end

        await _eventually(offset_at_end, what="the committed offset at the end of the log")
    finally:
        await ctx.stop()
    assert scenario.attempts["e2"] == 2


@requires_docker
@pytest.mark.backends(PG)
async def test_eda_kafka_stop_mid_handler_delivers_the_event_again_after_a_restart(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("eda-kafka-stop")
    first = Scenario(hold={"stuck": 60.0})
    config = _kafka_eda(kafka_url, topic, group, **{"shutdown-timeout": "0.5"})
    ctx = await _boot(relational_backend, config, _event_listener(first))
    try:
        send = _event_sender(ctx, topic)
        await _warm_up(first, send)
        await send("stuck")

        async def in_flight() -> bool:
            return "stuck" in first.started

        await _eventually(in_flight, what="the stuck handler to start")
    finally:
        await ctx.stop()
    assert await _bodies(relational_backend) == []

    second = Scenario()
    ctx = await _boot(relational_backend, config, _event_listener(second))
    try:

        async def redelivered() -> bool:
            return await _bodies(relational_backend) == ["stuck"]

        await _eventually(redelivered, what="the cancelled event delivered again after the restart")
    finally:
        await ctx.stop()


# -- C014: RabbitMQ @message_listener ----------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_rabbitmq_listener_that_fails_once_is_delivered_again_and_committed_once(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("rabbit-retry")
    scenario = Scenario(fail_first={"m2"})
    send = _rabbit_sender(amqp_url, topic)
    ctx = await _boot(relational_backend, _rabbit_messaging(amqp_url), _message_listener(topic, group, scenario))
    try:
        await _warm_up(scenario, send)
        for body in ("m1", "m2", "m3"):
            await send(body)

        async def all_rows() -> bool:
            return await _bodies(relational_backend) == ["m1", "m2", "m3"]

        await _eventually(all_rows, what="m1, m2 and m3 committed")
    finally:
        await ctx.stop()
    try:
        assert scenario.attempts["m2"] == 2
        assert await _rabbit_depth(amqp_url, group) == 0
        assert await _rabbit_depth(amqp_url, f"{group}.dlq") == 0
    finally:
        await _rabbit_delete(amqp_url, group, f"{group}.dlq")


@requires_docker
@pytest.mark.backends(PG)
async def test_rabbitmq_listener_that_keeps_failing_is_dead_lettered_after_max_attempts(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("rabbit-dlx")
    scenario = Scenario(always_fail={"poison"})
    send = _rabbit_sender(amqp_url, topic)
    ctx = await _boot(relational_backend, _rabbit_messaging(amqp_url), _message_listener(topic, group, scenario))
    try:
        await _warm_up(scenario, send)
        await send("poison")

        async def dead_lettered() -> bool:
            return await _rabbit_depth(amqp_url, f"{group}.dlq") == 1

        await _eventually(dead_lettered, what="the message in the dead-letter queue")
        await asyncio.sleep(0.5)
    finally:
        await ctx.stop()
    try:
        assert scenario.attempts["poison"] == 3
        assert await _bodies(relational_backend) == []
        [dead] = await _rabbit_queue(amqp_url, f"{group}.dlq")
        assert dead.body == b"poison"
        headers = {key: str(value) for key, value in (dead.headers or {}).items()}
        assert headers["x-exception"] == "RuntimeError"
        assert headers["x-dlt-attempts"] == "3"
        assert headers["x-original-topic"] == topic
        assert await _rabbit_depth(amqp_url, group) == 0
    finally:
        await _rabbit_delete(amqp_url, group, f"{group}.dlq")


# -- C015: RabbitMQ backlog vs a small pool ----------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_rabbitmq_backlog_runs_bounded_by_the_pool_and_loses_nothing(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("rabbit-backlog")
    bodies = [f"b{i:04d}" for i in range(1000)]
    await _rabbit_backlog(amqp_url, group, topic, bodies)
    scenario = Scenario(work=0.005)
    config = {
        **_rabbit_messaging(amqp_url),
        "pyfly.data.relational.pool.size": "2",
        "pyfly.data.relational.pool.max-overflow": "0",
        "pyfly.data.relational.pool.timeout": "1",
    }
    ctx = await _boot(relational_backend, config, _message_listener(topic, group, scenario))
    try:

        async def all_rows() -> bool:
            return len(await _bodies(relational_backend)) == len(bodies)

        await _eventually(all_rows, timeout=120, what="the whole backlog committed")
    finally:
        await ctx.stop()
    try:
        assert await _bodies(relational_backend) == bodies
        assert scenario.peak <= 2
        assert sum(scenario.attempts.values()) == len(bodies)  # no pool timeout, so no retry
        assert await _rabbit_depth(amqp_url, group) == 0
    finally:
        await _rabbit_delete(amqp_url, group, f"{group}.dlq")


# -- C013 on RabbitMQ: a handler cancelled at stop is requeued ---------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_rabbitmq_handler_cancelled_at_the_shutdown_timeout_is_delivered_after_a_restart(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("rabbit-cancel")
    first = Scenario(hold={"stuck": 60.0})
    send = _rabbit_sender(amqp_url, topic)
    config = _rabbit_messaging(amqp_url, **{"shutdown-timeout": "0.5"})
    ctx = await _boot(relational_backend, config, _message_listener(topic, group, first))
    try:
        await _warm_up(first, send)
        await send("stuck")

        async def in_flight() -> bool:
            return "stuck" in first.started

        await _eventually(in_flight, what="the stuck handler to start")
    finally:
        await ctx.stop()
    assert await _bodies(relational_backend) == []

    second = Scenario()
    ctx = await _boot(relational_backend, config, _message_listener(topic, group, second))
    try:

        async def redelivered() -> bool:
            return await _bodies(relational_backend) == ["stuck"]

        await _eventually(redelivered, what="the cancelled message delivered again after the restart")
    finally:
        await ctx.stop()
        await _rabbit_delete(amqp_url, group, f"{group}.dlq")


# -- C065: EDA RabbitMqEventBus ----------------------------------------------------------------------------


@requires_docker
@pytest.mark.backends(PG)
async def test_eda_rabbitmq_failure_is_retried_with_a_bound_and_dead_lettered(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    await relational_backend.create_tables(BrokerOrder)
    destination, group = _names("eda-rabbit")
    queue = f"{group}.{destination}"
    scenario = Scenario(always_fail={"poison"}, fail_first={"e2"})
    ctx = await _boot(
        relational_backend, _rabbit_eda(amqp_url, destination, group), _event_listener(scenario), DeadLetterStoreConfig
    )
    store = ctx.get_bean(EdaDeadLetterStore)  # type: ignore[type-abstract]
    try:
        send = _event_sender(ctx, destination)
        await _warm_up(scenario, send)
        await send("poison")
        await send("e2")

        async def settled() -> bool:
            return await _rabbit_depth(amqp_url, f"{queue}.dlq") == 1 and await _bodies(relational_backend) == ["e2"]

        await _eventually(settled, what="the poison event dead-lettered and e2 committed")
        await asyncio.sleep(1.0)  # an endless requeue loop would keep counting attempts here
    finally:
        await ctx.stop()
    try:
        assert scenario.attempts["poison"] == 3
        assert scenario.attempts["e2"] == 2
        assert await _rabbit_depth(amqp_url, queue) == 0
        # The application's EdaDeadLetterStore bean was injected into the auto-configured bus.
        [entry] = await store.list()
        assert (entry.event.payload, entry.error_type, entry.attempts) == ({"body": "poison"}, "RuntimeError", 3)
    finally:
        await _rabbit_delete(amqp_url, queue, f"{queue}.dlq")


# -- the listener's own @transactional settings shape the delivery's unit ----------------------------------

UNIT_SETTINGS: list[tuple[str, str, str]] = []


def _reporting_listener(topic: str, group: str) -> type:
    @service
    class ReportingListener:
        @message_listener(topic, group=group)
        @transactional(isolation=Isolation.SERIALIZABLE, read_only=True)
        async def on_report(self, message: Message) -> None:
            unit = current_unit_of_work()
            assert unit is not None
            isolation = (await unit.resource.execute(text("SHOW transaction_isolation"))).scalar_one()
            read_only = (await unit.resource.execute(text("SHOW transaction_read_only"))).scalar_one()
            UNIT_SETTINGS.append((message.value.decode(), str(isolation), str(read_only)))

    return ReportingListener


@requires_docker
@pytest.mark.backends(PG)
async def test_kafka_listener_s_isolation_and_read_only_reach_the_postgresql_transaction(
    relational_backend: RelationalBackend, kafka_url: str
) -> None:
    """A unit with the default settings ran this listener at READ COMMITTED, read-write: the listener's
    SERIALIZABLE and read-only were dropped when it joined."""
    UNIT_SETTINGS.clear()
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("kafka-settings")
    await _kafka_sender(kafka_url, topic)("report")
    ctx = await _boot(relational_backend, _kafka_messaging(kafka_url), _reporting_listener(topic, group))
    try:

        async def handled() -> bool:
            return bool(UNIT_SETTINGS)

        await _eventually(handled, timeout=45, what="the report handled")
        log_end = await _kafka_log_end(kafka_url, topic)

        async def committed() -> bool:
            return await _kafka_committed(kafka_url, group, topic) == log_end

        await _eventually(committed, what="the report's offset committed")
    finally:
        await ctx.stop()
    assert UNIT_SETTINGS == [("report", "serializable", "on")]


def _requires_new_listener(topic: str, group: str, scenario: Scenario, attempts: list[int]) -> type:
    @service
    class IndependentListener:
        def __init__(self, repo: BrokerOrderRepository) -> None:
            self.repo = repo

        @message_listener(topic, group=group)
        @transactional(propagation=Propagation.REQUIRES_NEW)
        async def on_order(self, message: Message) -> None:
            attempts.append(message.delivery_attempt)
            await scenario.handle(self.repo, message.value.decode())

    return IndependentListener


@requires_docker
@pytest.mark.backends(PG)
async def test_rabbitmq_requires_new_listener_needs_one_connection_per_delivery(
    relational_backend: RelationalBackend, amqp_url: str
) -> None:
    """Two handlers at once on a two-connection pool, each inside its transaction longer than the pool
    waits. Had the container opened a unit around the listener's REQUIRES_NEW, a delivery would hold both
    connections, and the next one would time out on the pool and be attempted again."""
    await relational_backend.create_tables(BrokerOrder)
    topic, group = _names("rabbit-requires-new")
    bodies = [f"n{i}" for i in range(6)]
    await _rabbit_backlog(amqp_url, group, topic, bodies)
    scenario = Scenario(work=1.0)
    attempts: list[int] = []
    config = {
        **_rabbit_messaging(amqp_url),
        "pyfly.data.relational.pool.size": "2",
        "pyfly.data.relational.pool.max-overflow": "0",
        "pyfly.data.relational.pool.timeout": "0.5",
    }
    ctx = await _boot(relational_backend, config, _requires_new_listener(topic, group, scenario, attempts))
    try:

        async def all_rows() -> bool:
            return len(await _bodies(relational_backend)) == len(bodies)

        await _eventually(all_rows, timeout=60, what="the whole backlog committed")
    finally:
        await ctx.stop()
    try:
        assert await _bodies(relational_backend) == bodies
        assert scenario.peak == 2  # both connections in use, one per delivery
        assert attempts == [1] * len(bodies)  # no pool timeout, so no retry
        assert await _rabbit_depth(amqp_url, f"{group}.dlq") == 0
    finally:
        await _rabbit_delete(amqp_url, group, f"{group}.dlq")


# -- transient failures on real servers --------------------------------------------------------------------


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_a_lock_timeout_is_a_transient_failure(relational_backend: RelationalBackend) -> None:
    """A row lock held by another transaction: PostgreSQL's lock_timeout (55P03) and InnoDB's lock wait
    timeout (1205) are failures of the moment, attempted again by the listener container."""
    engine = relational_backend.create_engine(poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE wp12_lock (id INTEGER PRIMARY KEY, n INTEGER)"))
        await conn.execute(text("INSERT INTO wp12_lock VALUES (1, 0)"))
    holder = await engine.connect()
    try:
        await holder.begin()
        await holder.execute(text("UPDATE wp12_lock SET n = n + 1 WHERE id = 1"))
        with pytest.raises(DBAPIError) as caught:
            async with engine.begin() as conn:
                if relational_backend.dialect == "postgresql":
                    await conn.execute(text("SET LOCAL lock_timeout = '200ms'"))
                else:
                    await conn.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
                await conn.execute(text("UPDATE wp12_lock SET n = n + 1 WHERE id = 1"))
    finally:
        await holder.rollback()
        await holder.close()
    assert is_transient_failure(caught.value)
    assert not is_transient_failure(ValueError("a deterministic failure"))


@pytest.mark.backends(PG)
async def test_a_serialization_failure_is_a_transient_failure(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine(poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE wp12_serial (id INTEGER PRIMARY KEY, n INTEGER)"))
        await conn.execute(text("INSERT INTO wp12_serial VALUES (1, 0)"))
    first = await engine.connect()
    second = await engine.connect()
    try:
        for conn in (first, second):
            await conn.execute(text("BEGIN ISOLATION LEVEL SERIALIZABLE"))
            await conn.execute(text("SELECT n FROM wp12_serial WHERE id = 1"))
        await first.execute(text("UPDATE wp12_serial SET n = 1 WHERE id = 1"))
        await first.commit()
        with pytest.raises(DBAPIError) as caught:
            await second.execute(text("UPDATE wp12_serial SET n = 2 WHERE id = 1"))
    finally:
        await first.close()
        await second.rollback()
        await second.close()
    assert is_transient_failure(caught.value)
