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
"""The RabbitMQ listener container on a real application: ``@message_listener`` + ``@transactional`` +
a repository on a SQLite file database, the broker an in-memory RabbitMQ that keeps queues, prefetch and
the unacknowledged deliveries of each channel (``brokers.FakeAmqpBroker``).

- C014: a failed delivery is republished to its queue after a back-off with its attempt count, and acked
  (the original) only after that; after the last attempt it is dead-lettered to ``<exchange>.dlx`` into
  ``<queue>.dlq``, never dropped;
- C015: each consumer channel gets a bounded prefetch, and the adapter's handlers are bounded by the
  datasource pool (one at a time on SQLite), so a backlog is processed without loss;
- C013: ``stop()`` waits for the deliveries in flight, and one it cancels at the timeout is requeued.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from pyfly.kernel.lifecycle import CONSUMER_PHASE, lifecycle_phase
from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter
from pyfly.messaging.listener_container import ATTEMPT_HEADER, FixedBackOff, ListenerContainerSettings, RetryPolicy
from pyfly.messaging.types import Message
from tests.messaging.brokers import FakeAmqpBroker
from tests.messaging.listener_app import (
    Behavior,
    boot,
    broker_bean,
    committed_bodies,
    eventually,
    message_listener_bean,
)
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE)

TOPIC = "orders"
QUEUE = "order-service"


def fast(*, max_attempts: int = 3, delay: float = 0.01, **settings: Any) -> ListenerContainerSettings:
    return ListenerContainerSettings(
        retry=RetryPolicy(max_attempts=max_attempts, backoff=FixedBackOff(delay)), **settings
    )


def adapter_on(broker: FakeAmqpBroker, settings: ListenerContainerSettings | None = None) -> RabbitMQAdapter:
    return RabbitMQAdapter("amqp://fake/", settings=settings or fast(), connection_factory=broker.connect)


def outcomes(broker: FakeAmqpBroker, body: bytes) -> list[tuple[str, Any]]:
    return [(outcome, attempt) for outcome, _queue, delivered, attempt in broker.outcomes if delivered == body]


async def test_a_failed_delivery_is_republished_with_its_attempt_and_acked_after_its_unit_commits(
    relational_backend: RelationalBackend,
) -> None:
    broker = FakeAmqpBroker()
    behavior = Behavior(fail_first={"m2"})
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), message_listener_bean(TOPIC, QUEUE, behavior))
    try:
        await eventually(lambda: bool(broker.channels) and QUEUE in broker.queues, what="the queue")
        await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
        for body in (b"m1", b"m2", b"m3"):
            broker.publish_to("pyfly", TOPIC, body)
        await eventually(lambda: behavior.finished.count("m2") == 1 and len(behavior.finished) == 3, what="done")
        await eventually(lambda: not broker.queues[QUEUE] and all(not c.unacked for c in broker.channels))
    finally:
        await ctx.stop()

    assert await committed_bodies(relational_backend) == ["m1", "m2", "m3"]  # m2's first attempt rolled back
    # The failed original is acked only once its republished copy (attempt 2) is in the queue.
    assert outcomes(broker, b"m2") == [("ack", None), ("ack", 2)]
    assert behavior.attempts["m2"] == 2
    retried = [message for message in behavior.messages if isinstance(message, Message) and message.value == b"m2"]
    assert [message.delivery_attempt for message in retried] == [1, 2]
    assert ATTEMPT_HEADER not in retried[1].headers
    assert broker.messages(f"{QUEUE}.dlq") == []


async def test_a_delivery_that_keeps_failing_is_dead_lettered_to_the_adapter_dlx(
    relational_backend: RelationalBackend,
) -> None:
    broker = FakeAmqpBroker()
    behavior = Behavior(always_fail={"poison"})
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), message_listener_bean(TOPIC, QUEUE, behavior))
    try:
        await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
        broker.publish_to("pyfly", TOPIC, b"poison", headers={"x-tenant": "acme"})
        await eventually(lambda: len(broker.messages(f"{QUEUE}.dlq")) == 1, what="the dead letter")
    finally:
        await ctx.stop()

    assert behavior.attempts["poison"] == 3
    assert await committed_bodies(relational_backend) == []
    [dead] = broker.messages(f"{QUEUE}.dlq")
    assert dead.body == b"poison"
    assert dead.headers["x-tenant"] == "acme"
    assert dead.headers["x-original-topic"] == TOPIC
    assert dead.headers["x-exception"] == "RuntimeError"
    assert dead.headers["x-dlt-attempts"] == "3"
    assert dead.headers["x-dlt-source-queue"] == QUEUE
    assert ATTEMPT_HEADER not in dead.headers
    assert broker.route("pyfly.dlx", QUEUE) == {f"{QUEUE}.dlq"}
    assert broker.queues[QUEUE] == type(broker.queues[QUEUE])()  # nothing requeued, nothing lost
    assert [outcome for outcome, _ in outcomes(broker, b"poison")] == ["ack", "ack", "ack"]


async def test_prefetch_and_the_pool_bound_the_handlers_of_a_backlog(relational_backend: RelationalBackend) -> None:
    """300 queued messages: the consumer channel's prefetch is 20, and on SQLite (one writer) the adapter
    runs one handler at a time, so every message commits; none is rejected."""
    broker = FakeAmqpBroker()
    broker.declare_queue(QUEUE)
    broker.bindings.setdefault("pyfly", {}).setdefault(TOPIC, set()).add(QUEUE)
    bodies = [f"b{i:03d}" for i in range(300)]
    for body in bodies:
        broker.publish_to("pyfly", TOPIC, body.encode())
    behavior = Behavior(work=0.001)
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), message_listener_bean(TOPIC, QUEUE, behavior))
    try:
        await eventually(lambda: len(behavior.finished) == len(bodies), timeout=60, what="the backlog")
        await eventually(lambda: all(not c.unacked for c in broker.channels))
        consumer_channels = [channel for channel in broker.channels if channel.qos_calls]
    finally:
        await ctx.stop()
    assert await committed_bodies(relational_backend) == bodies
    assert behavior.peak == 1
    assert [channel.qos_calls for channel in consumer_channels] == [[20]]
    assert consumer_channels[0].max_unacked == 20
    assert {outcome for outcome, *_ in broker.outcomes} == {"ack"}


async def test_an_explicit_concurrency_bounds_the_handlers_of_every_consumer(
    relational_backend: RelationalBackend,
) -> None:
    """Two consumers of one adapter share its limit: never more than three handlers at once."""
    broker = FakeAmqpBroker()
    for queue, topic in ((QUEUE, TOPIC), ("audit", "audit")):
        broker.declare_queue(queue)
        broker.bindings.setdefault("pyfly", {}).setdefault(topic, set()).add(queue)
        for index in range(30):
            broker.publish_to("pyfly", topic, f"{topic}-{index}".encode())
    running = 0
    peak = 0
    done: list[str] = []

    async def handler(message: Message) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.005)
        running -= 1
        done.append(message.value.decode())

    adapter = adapter_on(broker, fast(concurrency=3, transactional=False))
    await adapter.subscribe(TOPIC, handler, group=QUEUE)
    await adapter.subscribe("audit", handler, group="audit")
    await adapter.start()
    try:
        await eventually(lambda: len(done) == 60, what="both queues drained")
    finally:
        await adapter.stop()
    assert peak == 3


async def test_stop_waits_for_a_delivery_in_flight_and_acks_it(relational_backend: RelationalBackend) -> None:
    broker = FakeAmqpBroker()
    gate = asyncio.Event()
    behavior = Behavior(gates={"slow": gate})
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), message_listener_bean(TOPIC, QUEUE, behavior))
    await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
    broker.publish_to("pyfly", TOPIC, b"slow")
    await behavior.started_event("slow").wait()
    stopping = asyncio.create_task(ctx.stop())
    await asyncio.sleep(0.1)
    assert not stopping.done()
    gate.set()
    await stopping
    assert await committed_bodies(relational_backend) == ["slow"]
    assert outcomes(broker, b"slow") == [("ack", None)]
    assert broker.messages(QUEUE) == []


async def test_stop_requeues_a_delivery_still_running_at_the_timeout(relational_backend: RelationalBackend) -> None:
    broker = FakeAmqpBroker()
    stuck = Behavior(gates={"stuck": asyncio.Event()})
    settings = fast(shutdown_timeout=0.2)
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(broker, settings)), message_listener_bean(TOPIC, QUEUE, stuck)
    )
    await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
    broker.publish_to("pyfly", TOPIC, b"stuck")
    await stuck.started_event("stuck").wait()
    await ctx.stop()

    assert await committed_bodies(relational_backend) == []  # cancelled inside its unit: rolled back
    assert outcomes(broker, b"stuck") == [("requeue", None)]
    [waiting] = broker.messages(QUEUE)
    assert waiting.body == b"stuck" and waiting.redelivered

    restarted = Behavior()
    ctx = await boot(
        relational_backend, broker_bean(adapter_on(broker, settings)), message_listener_bean(TOPIC, QUEUE, restarted)
    )
    try:
        await eventually(lambda: restarted.finished == ["stuck"], what="delivered again after the restart")
        await eventually(lambda: not broker.messages(QUEUE) and all(not c.unacked for c in broker.channels))
    finally:
        await ctx.stop()
    assert await committed_bodies(relational_backend) == ["stuck"]


async def test_a_listener_s_dead_letter_topic_is_used_when_a_queue_takes_it(
    relational_backend: RelationalBackend,
) -> None:
    broker = FakeAmqpBroker()
    broker.declare_queue("failed-orders")
    broker.bindings.setdefault("pyfly", {}).setdefault("orders.failed", set()).add("failed-orders")
    behavior = Behavior(always_fail={"poison"})
    listener = message_listener_bean(TOPIC, QUEUE, behavior, retries=0, dead_letter_topic="orders.failed")
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), listener)
    try:
        await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
        broker.publish_to("pyfly", TOPIC, b"poison")
        await eventually(lambda: broker.bodies("failed-orders") == [b"poison"], what="the listener's dead letter")
    finally:
        await ctx.stop()
    assert behavior.attempts["poison"] == 1
    assert broker.messages(f"{QUEUE}.dlq") == []


async def test_a_dead_letter_topic_no_queue_takes_falls_back_to_the_adapter_dlq(
    relational_backend: RelationalBackend,
) -> None:
    """An unroutable dead-letter publish raises (the consumer channel has ``on_return_raises``) instead of
    vanishing, and the default dead-letter queue keeps the message."""
    broker = FakeAmqpBroker()
    behavior = Behavior(always_fail={"poison"})
    listener = message_listener_bean(TOPIC, QUEUE, behavior, retries=0, dead_letter_topic="orders.nobody-reads")
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), listener)
    try:
        await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
        broker.publish_to("pyfly", TOPIC, b"poison")
        await eventually(lambda: broker.bodies(f"{QUEUE}.dlq") == [b"poison"], what="the fallback dead letter")
    finally:
        await ctx.stop()


async def test_a_failed_retry_publish_requeues_the_message(relational_backend: RelationalBackend) -> None:
    broker = FakeAmqpBroker()
    behavior = Behavior(fail_first={"m"})
    ctx = await boot(relational_backend, broker_bean(adapter_on(broker)), message_listener_bean(TOPIC, QUEUE, behavior))
    try:
        await eventually(lambda: broker.route("pyfly", TOPIC) == {QUEUE}, what="the binding")
        consumer_channel = next(channel for channel in broker.channels if channel.qos_calls)
        consumer_channel.publish_failures = 1
        broker.publish_to("pyfly", TOPIC, b"m")
        await eventually(lambda: behavior.finished == ["m"], what="delivered again and handled")
    finally:
        await ctx.stop()
    assert outcomes(broker, b"m") == [("requeue", None), ("ack", None)]
    assert await committed_bodies(relational_backend) == ["m"]


def test_the_adapter_is_a_consumer_phase_bean() -> None:
    adapter = RabbitMQAdapter("amqp://fake/")
    assert lifecycle_phase(adapter) == CONSUMER_PHASE
    assert adapter.manages_listener_errors is True


async def test_an_unexpected_error_in_a_delivery_requeues_the_message(caplog: pytest.LogCaptureFixture) -> None:
    from pyfly.messaging.listener_container import ConcurrencyLimit, RabbitDeadLetter, RabbitListenerContainer

    broker = FakeAmqpBroker()
    connection = await broker.connect("amqp://fake/")

    def broken_size() -> int:
        raise RuntimeError("cannot size the limit")

    async def handler(message: Any) -> None:
        pass

    container: RabbitListenerContainer[Any] = RabbitListenerContainer(
        connection=connection,
        queue=QUEUE,
        bindings=[("pyfly", TOPIC)],
        convert=lambda message, attempt: message.body,
        handler=handler,
        dead_letters=[RabbitDeadLetter("pyfly.dlx", QUEUE, f"{QUEUE}.dlq")],
        settings=fast(transactional=False),
        limit=ConcurrencyLimit(broken_size),
    )
    await container.start()
    try:
        with caplog.at_level(logging.ERROR, logger="pyfly.messaging.listener_container"):
            broker.publish_to("pyfly", TOPIC, b"m")
            await eventually(lambda: ("requeue", QUEUE, b"m", None) in broker.outcomes, what="the requeue")
    finally:
        await container.stop()
    assert any("listener_delivery_error" in record.getMessage() for record in caplog.records)
    assert broker.bodies(QUEUE) == [b"m"]  # back in its queue, not stuck unacknowledged


@pytest.mark.parametrize("transport", ["kafka", "rabbitmq"])
async def test_a_datasource_the_settings_name_must_exist_when_the_consumer_starts(
    relational_backend: RelationalBackend, transport: str
) -> None:
    from pyfly.data.transaction import IllegalTransactionStateError
    from pyfly.messaging.adapters.kafka import KafkaAdapter
    from tests.messaging.brokers import FakeKafkaCluster

    ctx = await boot(relational_backend)
    try:
        settings = fast(datasource="reporting")
        cluster = FakeKafkaCluster()
        adapter: Any = (
            KafkaAdapter(
                "fake:9092", settings=settings, consumer_factory=cluster.consumer, producer_factory=cluster.producer
            )
            if transport == "kafka"
            else adapter_on(FakeAmqpBroker(), settings)
        )

        async def handler(message: Message) -> None:
            pass

        await adapter.subscribe(TOPIC, handler, group=QUEUE)
        with pytest.raises(IllegalTransactionStateError, match="reporting"):
            await adapter.start()
        await adapter.stop()
    finally:
        await ctx.stop()
