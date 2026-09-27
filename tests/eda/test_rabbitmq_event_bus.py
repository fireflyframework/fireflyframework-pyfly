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
"""Tests for :class:`RabbitMqEventBus` against an in-memory RabbitMQ that keeps queues, prefetch and the
unacknowledged deliveries of each channel (``tests.messaging.brokers.FakeAmqpBroker``).

C065 (26.09.08): a failing handler used to be requeued at once, forever, with every message of the backlog
running at the same time (no prefetch). The bus now consumes through the listener container: a prefetch
per consumer channel, a concurrency limit, a bounded number of attempts with a delay, then the
dead-letter queue ``<queue>.dlq`` behind ``<exchange>.dlx``.

The bus declares its queues when it starts but consumes only once a handler subscribed: the application
context starts it before it subscribes the ``@event_listener`` methods, and a backlog delivered in between
matched no handler and was acked, lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pyfly.container.stereotypes import service
from pyfly.context.lifecycle import post_construct
from pyfly.core.config import Config
from pyfly.data.transaction import Isolation, UnitOfWork, current_unit_of_work
from pyfly.data.transactional import transactional
from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.dlq import InMemoryEdaDeadLetterStore
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.serializers import JsonEventSerializer
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE, lifecycle_phase
from pyfly.messaging.listener_container import ATTEMPT_HEADER, FixedBackOff, ListenerContainerSettings, RetryPolicy
from tests.messaging.brokers import FakeAmqpBroker
from tests.messaging.listener_app import (
    Behavior,
    Delivered,
    DeliveredRepository,
    boot,
    committed_bodies,
    event_bus_bean,
    event_listener_bean,
    eventually,
)
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

FAST = ListenerContainerSettings(retry=RetryPolicy(max_attempts=3, backoff=FixedBackOff(0.01)), transactional=False)
QUEUE = "pyfly-default.orders"


def _bus(broker: FakeAmqpBroker, **kwargs: Any) -> RabbitMqEventBus:
    kwargs.setdefault("settings", FAST)
    kwargs.setdefault("destinations", ["orders"])
    return RabbitMqEventBus(url="amqp://fake/", connection_factory=broker.connect, **kwargs)


def _envelope(event_type: str = "order.created", **payload: Any) -> bytes:
    return JsonEventSerializer().serialize(EventEnvelope(event_type=event_type, payload=payload, destination="orders"))


def _settled(broker: FakeAmqpBroker) -> bool:
    return not broker.queues.get(QUEUE) and all(not channel.unacked for channel in broker.channels)


class TestRabbitMqEventBus:
    def test_protocol_compliance(self) -> None:
        bus = RabbitMqEventBus()
        assert isinstance(bus, EventPublisher)
        assert lifecycle_phase(bus) == CONSUMER_PHASE

    def test_subscribe_registers_handlers(self) -> None:
        bus = RabbitMqEventBus()

        async def h1(envelope: EventEnvelope) -> None:
            pass

        async def h2(envelope: EventEnvelope) -> None:
            pass

        bus.subscribe("order.*", h1)
        bus.subscribe("payment.*", h2)
        assert bus._handlers == [("order.*", h1), ("payment.*", h2)]

    async def test_publish_builds_a_persistent_envelope_message(self) -> None:
        import aio_pika

        broker = FakeAmqpBroker()
        bus = _bus(broker)
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("order.*", handler)
        await bus.start()
        try:
            exchange = bus._exchange
            await bus.publish("orders", "order.created", {"id": 1}, headers={"x-tenant": "acme"})
            await eventually(lambda: _settled(broker) and len(received) == 1, what="the message consumed")
        finally:
            await bus.stop()
        [(message, routing_key)] = exchange.published
        assert routing_key == "orders"
        body = json.loads(message.body.decode("utf-8"))
        assert (body["event_type"], body["payload"], body["destination"]) == ("order.created", {"id": 1}, "orders")
        assert body["headers"] == {"x-tenant": "acme"}
        assert message.message_id == body["event_id"]
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT

    async def test_publish_auto_starts(self) -> None:
        bus = RabbitMqEventBus()
        started = {"v": False}

        async def fake_start() -> None:
            started["v"] = True
            bus._exchange = AsyncMock()
            bus._started = True

        bus.start = fake_start  # type: ignore[method-assign]
        await bus.publish("t", "e", {})
        assert started["v"] is True

    async def test_start_declares_the_queues_their_dead_letter_queues_and_a_prefetch(self) -> None:
        broker = FakeAmqpBroker()
        bus = _bus(broker, exchange_name="test-exchange", destinations=["orders", "payments"], group="svc")
        await bus.start()
        try:
            assert broker.route("test-exchange", "orders") == {"svc.orders"}
            assert broker.route("test-exchange", "payments") == {"svc.payments"}
            assert broker.route("test-exchange.dlx", "svc.orders") == {"svc.orders.dlq"}
            assert broker.route("test-exchange.dlx", "svc.payments") == {"svc.payments.dlq"}
            consumer_channels = [channel for channel in broker.channels if channel.qos_calls]
            assert [channel.qos_calls for channel in consumer_channels] == [[20], [20]]
        finally:
            await bus.stop()

    async def test_the_bus_consumes_only_once_a_handler_subscribed(self) -> None:
        """An event published before any handler subscribed waits in its queue, and is not acked unseen."""
        broker = FakeAmqpBroker()
        bus = _bus(broker)
        received: list[str] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope.event_type)

        await bus.start()
        try:
            await bus.publish("orders", "order.created", {"id": 1})
            await asyncio.sleep(0.05)
            assert broker.bodies(QUEUE) and not broker.outcomes
            assert not any(channel.consumers for channel in broker.channels)
            bus.subscribe("order.*", handler)
            await eventually(lambda: received == ["order.created"] and _settled(broker), what="the delivery")
        finally:
            await bus.stop()
        assert [outcome for outcome, *_ in broker.outcomes] == ["ack"]

    async def test_start_is_idempotent(self) -> None:
        broker = FakeAmqpBroker()
        bus = _bus(broker)
        await bus.start()
        await bus.start()
        try:
            assert len(broker.connections) == 1
        finally:
            await bus.stop()

    async def test_stop_closes_the_connection(self) -> None:
        broker = FakeAmqpBroker()
        bus = _bus(broker)
        await bus.start()
        await bus.stop()
        assert broker.connections[0].closed
        assert bus._started is False
        assert bus._connection is None

    async def test_stop_when_not_started_is_safe(self) -> None:
        bus = RabbitMqEventBus()
        await bus.stop()
        assert bus._started is False

    async def test_start_goes_through_aio_pika_connect_robust_by_default(self) -> None:
        broker = FakeAmqpBroker()
        bus = RabbitMqEventBus(url="amqp://test/", destinations=["orders"])
        with patch("aio_pika.connect_robust", side_effect=broker.connect) as connect:
            await bus.start()
        try:
            connect.assert_awaited_once_with("amqp://test/")
        finally:
            await bus.stop()


class TestDispatch:
    async def test_matching_subscribers_get_the_event_and_the_message_is_acked(self) -> None:
        broker = FakeAmqpBroker()
        received: list[str] = []
        other: list[str] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope.event_type)

        async def payments(envelope: EventEnvelope) -> None:
            other.append(envelope.event_type)

        bus = _bus(broker)
        bus.subscribe("order.*", handler)
        bus.subscribe("payment.*", payments)
        await bus.start()
        try:
            broker.publish_to("pyfly", "orders", _envelope())
            broker.publish_to("pyfly", "orders", _envelope("shipment.sent"))  # no handler: not a failure
            await eventually(lambda: len(broker.outcomes) == 2, what="both settled")
        finally:
            await bus.stop()
        assert received == ["order.created"]
        assert other == []
        assert [outcome for outcome, *_ in broker.outcomes] == ["ack", "ack"]

    async def test_a_message_that_cannot_be_read_is_dead_lettered_at_once(self, caplog: Any) -> None:
        broker = FakeAmqpBroker()
        bus = _bus(broker)

        async def handler(envelope: EventEnvelope) -> None:
            pass

        bus.subscribe("order.*", handler)
        with caplog.at_level(logging.WARNING, logger="pyfly.messaging.listener_container"):
            await bus.start()
            try:
                broker.publish_to("pyfly", "orders", b"not-valid-json")
                await eventually(lambda: broker.bodies(f"{QUEUE}.dlq") == [b"not-valid-json"], what="the dead letter")
            finally:
                await bus.stop()
        [dead] = broker.messages(f"{QUEUE}.dlq")
        assert dead.headers["x-dlt-reason"] == "EnvelopeDecodeError"
        assert dead.headers["x-dlt-attempts"] == "1"
        assert [outcome for outcome, *_ in broker.outcomes] == ["ack"]  # moved to the DLQ, not dropped
        assert any("listener_delivery_dead_lettered" in r.getMessage() for r in caplog.records)

    async def test_a_failing_handler_is_retried_with_a_delay_a_bounded_number_of_times_then_dead_lettered(
        self,
    ) -> None:
        """C065: three attempts (republished with their attempt count), not an endless requeue loop."""
        broker = FakeAmqpBroker()
        attempts: list[int] = []
        sibling: list[int] = []

        async def bad_handler(envelope: EventEnvelope) -> None:
            attempts.append(1)
            raise RuntimeError("boom")

        async def audit(envelope: EventEnvelope) -> None:
            sibling.append(1)

        bus = _bus(broker)
        bus.subscribe("order.*", audit)
        bus.subscribe("order.*", bad_handler)
        await bus.start()
        try:
            broker.publish_to("pyfly", "orders", _envelope(id=1))
            await eventually(lambda: len(broker.messages(f"{QUEUE}.dlq")) == 1, what="the dead letter")
            await asyncio.sleep(0.1)  # an endless requeue loop would keep counting here
        finally:
            await bus.stop()
        assert len(attempts) == 3
        assert len(sibling) == 3  # the sibling ran in the same (rolled back) attempt each time
        assert [(outcome, attempt) for outcome, _q, _b, attempt in broker.outcomes] == [
            ("ack", None),
            ("ack", 2),
            ("ack", 3),
        ]
        [dead] = broker.messages(f"{QUEUE}.dlq")
        assert dead.headers["x-dlt-attempts"] == "3"
        assert ATTEMPT_HEADER not in dead.headers

    async def test_the_dead_letter_store_records_the_failed_event(self) -> None:
        broker = FakeAmqpBroker()
        store = InMemoryEdaDeadLetterStore()

        async def bad_handler(envelope: EventEnvelope) -> None:
            raise RuntimeError("projection is broken")

        bus = _bus(broker, dead_letter_store=store)
        bus.subscribe("order.*", bad_handler)
        await bus.start()
        try:
            broker.publish_to("pyfly", "orders", _envelope(id=9))
            await eventually(lambda: len(broker.messages(f"{QUEUE}.dlq")) == 1, what="the dead letter")
        finally:
            await bus.stop()
        [entry] = await store.list()
        assert (entry.event.payload, entry.error_type, entry.attempts) == ({"id": 9}, "RuntimeError", 3)

    async def test_a_failing_dead_letter_store_neither_loops_nor_floods_the_dlq(self, caplog: Any) -> None:
        """The event is in the DLQ before the store is asked: a store that fails (its table is unreachable
        during a database outage) is logged and counted, and the handler is not run again for it."""
        broker = FakeAmqpBroker()
        runs: list[int] = []

        class BrokenStore(InMemoryEdaDeadLetterStore):
            calls = 0

            async def add(self, entry: Any) -> None:
                BrokenStore.calls += 1
                raise RuntimeError("the dead-letter table is unreachable")

        async def bad_handler(envelope: EventEnvelope) -> None:
            runs.append(1)
            raise ValueError("bad event")

        settings = ListenerContainerSettings(retry=RetryPolicy(max_attempts=1), transactional=False)
        bus = _bus(broker, settings=settings, dead_letter_store=BrokenStore())
        bus.subscribe("order.*", bad_handler)
        await bus.start()
        try:
            with caplog.at_level(logging.ERROR):
                broker.publish_to("pyfly", "orders", _envelope(id=1))
                await eventually(lambda: len(broker.messages(f"{QUEUE}.dlq")) == 1, what="the dead letter")
                await asyncio.sleep(0.2)
        finally:
            await bus.stop()
        assert (len(runs), BrokenStore.calls, bus.dead_letter_store_failures) == (1, 1, 1)
        assert len(broker.messages(f"{QUEUE}.dlq")) == 1
        assert [outcome for outcome, *_ in broker.outcomes] == ["ack"]
        assert any("dead_letter_store_failed" in record.getMessage() for record in caplog.records)

    async def test_a_publish_after_stop_closes_the_connection_it_opened(self) -> None:
        """A ``@pre_destroy`` announcing the shutdown publishes on a connection of its own, closed again:
        nothing would close one the bus kept open after it stopped."""
        broker = FakeAmqpBroker()
        bus = _bus(broker)
        await bus.start()
        await bus.stop()
        broker.declare_queue("goodbye")
        broker.bindings.setdefault("pyfly", {}).setdefault("goodbye", set()).add("goodbye")
        await bus.publish("goodbye", "app.stopping", {})
        assert len(broker.bodies("goodbye")) == 1
        assert len(broker.connections) == 2 and all(connection.closed for connection in broker.connections)
        assert bus._connection is None and not bus._started

    async def test_a_backlog_is_bounded_by_the_prefetch_and_the_concurrency_limit(self) -> None:
        broker = FakeAmqpBroker()
        broker.declare_queue(QUEUE)
        broker.bindings.setdefault("pyfly", {}).setdefault("orders", set()).add(QUEUE)
        for index in range(100):
            broker.publish_to("pyfly", "orders", _envelope(id=index))
        running = 0
        peak = 0
        done: list[int] = []

        async def handler(envelope: EventEnvelope) -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.002)
            running -= 1
            done.append(envelope.payload["id"])

        settings = ListenerContainerSettings(retry=FAST.retry, transactional=False, prefetch=10, concurrency=4)
        bus = _bus(broker, settings=settings)
        bus.subscribe("order.*", handler)
        await bus.start()
        try:
            await eventually(lambda: len(done) == 100, what="the backlog")
            consumer = next(channel for channel in broker.channels if channel.consumers)
        finally:
            await bus.stop()
        assert peak == 4
        assert consumer.max_unacked == 10
        assert sorted(done) == list(range(100))

    def test_auto_configuration_reads_the_listener_settings(self) -> None:
        config = Config(
            {
                "pyfly": {
                    "eda": {
                        "provider": "rabbitmq",
                        "rabbitmq": {"url": "amqp://x/", "prefetch": "7", "dead-letter-exchange": "dead"},
                        "listener": {"retry": {"max-attempts": "2"}},
                    }
                }
            }
        )
        bus = EdaAutoConfiguration().event_publisher(config)
        assert isinstance(bus, RabbitMqEventBus)
        assert (bus._settings.prefetch, bus._settings.retry.max_attempts) == (7, 2)
        assert bus._dead_letter_exchange == "dead"


@pytest.mark.backends(SQLITE_FILE)
async def test_a_transactional_event_listener_that_fails_once_commits_once(
    relational_backend: RelationalBackend,
) -> None:
    broker = FakeAmqpBroker()
    behavior = Behavior(fail_first={"e2"})
    bus = _bus(broker, settings=ListenerContainerSettings(retry=FAST.retry))
    ctx = await boot(relational_backend, event_bus_bean(bus), event_listener_bean(behavior))
    try:
        for body in ("e1", "e2", "e3"):
            broker.publish_to("pyfly", "orders", _envelope(body=body))
        await eventually(lambda: len(behavior.finished) == 3 and _settled(broker), what="all three")
    finally:
        await ctx.stop()
    assert await committed_bodies(relational_backend) == ["e1", "e2", "e3"]
    assert behavior.attempts["e2"] == 2


@pytest.mark.backends(SQLITE_FILE)
async def test_deliveries_do_not_inherit_the_transaction_of_the_publisher(
    relational_backend: RelationalBackend,
) -> None:
    """The bus starts on its first publish, inside a request's ``@transactional``, and the delivery is
    dispatched from that task: the container runs it detached, in a unit of its own."""
    from pyfly.data.transaction import current_unit_of_work
    from pyfly.data.transactional import transactional
    from tests.messaging.listener_app import DeliveredRepository

    broker = FakeAmqpBroker()
    behavior = Behavior()
    bus = _bus(broker, settings=ListenerContainerSettings(retry=FAST.retry))
    ctx = await boot(relational_backend)
    request_units: list[int] = []
    try:
        repo = ctx.get_bean(DeliveredRepository)
        bus.subscribe("order.*", lambda envelope: behavior.handle(repo, str(envelope.payload["body"])))

        @transactional
        async def place_order() -> None:
            unit = current_unit_of_work()
            assert unit is not None
            request_units.append(unit.id)
            await bus.publish("orders", "order.created", {"body": "late"})

        await place_order()
        await eventually(lambda: behavior.finished == ["late"] and _settled(broker), what="the event handled")
    finally:
        await bus.stop()
        await ctx.stop()
    [handler_unit] = behavior.units
    assert handler_unit is not None and handler_unit != request_units[0]
    assert await committed_bodies(relational_backend) == ["late"]


@service
class SlowToStart:
    """A bean whose initialization awaits: the context runs it after the bus started, before the
    @event_listener methods are subscribed."""

    @post_construct
    async def warm_up(self) -> None:
        await asyncio.sleep(0.05)


@pytest.mark.backends(SQLITE_FILE)
async def test_a_backlog_waiting_before_the_application_starts_reaches_its_listeners(
    relational_backend: RelationalBackend,
) -> None:
    """The context starts the bus, then initializes the other beans (one of them slowly), then subscribes
    the @event_listener methods: nothing is consumed before they subscribed, so nothing is acked unseen."""
    broker = FakeAmqpBroker()
    broker.declare_queue(QUEUE)
    broker.bindings.setdefault("pyfly", {}).setdefault("orders", set()).add(QUEUE)
    bodies = [f"e{index}" for index in range(5)]
    for body in bodies:
        broker.publish_to("pyfly", "orders", _envelope(body=body))
    behavior = Behavior()
    bus = _bus(broker, settings=ListenerContainerSettings(retry=FAST.retry))
    ctx = await boot(relational_backend, event_bus_bean(bus), SlowToStart, event_listener_bean(behavior))
    try:
        await eventually(lambda: len(behavior.finished) == 5 and _settled(broker), what="the backlog")
    finally:
        await ctx.stop()
    assert sorted(behavior.finished) == bodies
    assert await committed_bodies(relational_backend) == bodies


SERIALIZABLE_UNITS: list[UnitOfWork | None] = []


@service
class SerializableEvents:
    def __init__(self, repo: DeliveredRepository) -> None:
        self.repo = repo

    @event_listener(["order.*"])
    @transactional(isolation=Isolation.SERIALIZABLE)
    async def on_event(self, envelope: EventEnvelope) -> None:
        SERIALIZABLE_UNITS.append(current_unit_of_work())
        await self.repo.save(Delivered(body=str(envelope.payload["body"])))


@pytest.mark.backends(SQLITE_FILE)
async def test_an_event_listener_s_transactional_settings_shape_the_delivery_s_unit(
    relational_backend: RelationalBackend,
) -> None:
    SERIALIZABLE_UNITS.clear()
    units = SERIALIZABLE_UNITS
    broker = FakeAmqpBroker()
    bus = _bus(broker, settings=ListenerContainerSettings(retry=FAST.retry))
    ctx = await boot(relational_backend, event_bus_bean(bus), SerializableEvents)
    try:
        broker.publish_to("pyfly", "orders", _envelope(body="e1"))
        await eventually(lambda: len(units) == 1 and _settled(broker), what="the delivery")
    finally:
        await ctx.stop()
    [unit] = units
    assert unit is not None and unit.isolation is Isolation.SERIALIZABLE and unit.suspended is None
    assert await committed_bodies(relational_backend) == ["e1"]
