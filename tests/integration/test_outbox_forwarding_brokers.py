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
"""Kafka and RabbitMQ made transactional by the outbox layer (WP09b), with a real broker and a real database.

Publishing to a broker inside a unit of work was a dual write: the broker kept an event whose unit rolled back,
and lost one whose process died between the commit and the publish. ``TransactionalEventPublisher`` appends the
event in the unit and ``OutboxForwarder`` publishes it once the unit committed. Here, on a Kafka and a RabbitMQ
testcontainer, with SQLite file and PostgreSQL as the application database, read back from the broker itself:

- a unit that rolls back publishes nothing; one that commits publishes exactly one message;
- a forwarder process killed between the broker publish and the settle leaves the event to be published once
  more, when its lease ends, with the same ``x-pyfly-event-id``;
- a broker that cannot be reached is retried, then the event is dead-lettered, and forwarding resumes when the
  broker is back;
- two instances forwarding from one database publish each event once;
- in an application context (``pyfly.eda.outbox.enabled``), a committed ``@transactional`` method's event goes
  through the broker to the ``@event_listener`` once, and a rolled-back one's never.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import textwrap
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import Identity, Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, transactional
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox import OutboxTables, SqlOutboxStore
from pyfly.eda.outbox_forwarding import OutboxForwarder, TransactionalEventPublisher
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.messaging.listener_container import FixedBackOff, RetryPolicy
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend
from tests.support.brokers import Broker, eventually

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.brokers, pytest.mark.backends(SQLITE_FILE, PG)]


class BrokerOrder(Base):
    __tablename__ = "wp09b_broker_order"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class BrokerOrderRepository(Repository[BrokerOrder, int]):
    pass


# ---------------------------------------------------------------------------------------------------------------
# The brokers, read back directly
# ---------------------------------------------------------------------------------------------------------------


@pytest.fixture(params=["kafka", "rabbitmq"])
async def broker(request: pytest.FixtureRequest) -> AsyncIterator[Broker]:
    url: str = request.getfixturevalue("kafka_url" if request.param == "kafka" else "amqp_url")
    lane = Broker(request.param, url)
    await lane.listen()
    try:
        yield lane
    finally:
        await lane.cleanup()


def _event_ids(envelopes: list[EventEnvelope]) -> list[str]:
    return [envelope.headers[EVENT_ID_HEADER] for envelope in envelopes]


# ---------------------------------------------------------------------------------------------------------------
# Rollback and commit
# ---------------------------------------------------------------------------------------------------------------


async def test_a_unit_that_rolls_back_publishes_nothing_and_one_that_commits_publishes_once(
    relational_backend: RelationalBackend, broker: Broker
) -> None:
    engine = relational_backend.create_engine()
    await relational_backend.create_tables(BrokerOrder)
    publisher = TransactionalEventPublisher(
        broker.transport(), SqlOutboxStore(engine), name=broker.name, poll_interval=0.2
    )
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    await publisher.start()
    try:
        with pytest.raises(RuntimeError, match="payment declined"):
            async with template.transaction() as unit:
                assert unit is not None
                unit.resource.add(BrokerOrder(name="rolled back"))
                await publisher.publish(broker.destination, "order.placed", {"n": "rolled back"})
                raise RuntimeError("payment declined")
        async with template.transaction() as unit:
            assert unit is not None
            unit.resource.add(BrokerOrder(name="committed"))
            await publisher.publish(broker.destination, "order.placed", {"n": "committed"}, {"x-trace": "t-1"})
        await eventually(lambda: publisher.relay.counters.delivered == 1)
    finally:
        await publisher.stop()

    messages = await broker.messages(expected=1)
    assert [message.payload for message in messages] == [{"n": "committed"}]
    (message,) = messages
    assert (message.event_type, message.destination) == ("order.placed", broker.destination)
    assert message.headers["x-trace"] == "t-1"
    assert message.headers[EVENT_ID_HEADER]
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(BrokerOrder))).scalar_one() == 1
    assert await publisher.pending() == []


async def test_without_the_outbox_a_unit_that_rolls_back_still_published_its_event(
    relational_backend: RelationalBackend, broker: Broker
) -> None:
    """The dual write the layer removes: the transport alone publishes at once, and the rollback cannot take the
    event back."""
    engine = relational_backend.create_engine()
    transport = broker.transport()
    try:
        with pytest.raises(RuntimeError, match="payment declined"):
            async with TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine)).transaction():
                await transport.publish(broker.destination, "order.placed", {"n": "rolled back"})
                raise RuntimeError("payment declined")
    finally:
        await transport.stop()

    assert [message.payload for message in await broker.messages(expected=1)] == [{"n": "rolled back"}]


# ---------------------------------------------------------------------------------------------------------------
# A forwarder killed between the broker publish and the settle
# ---------------------------------------------------------------------------------------------------------------

_DYING_FORWARDER = textwrap.dedent(
    """
    import asyncio
    import os
    import sys

    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.eda.outbox import SqlOutboxStore
    from pyfly.eda.outbox_forwarding import OutboxForwarder


    async def main(database_url, broker, broker_url, destination):
        if broker == "kafka":
            from pyfly.eda.adapters.kafka import KafkaEventBus

            transport = KafkaEventBus(bootstrap_servers=broker_url, topics=[destination], group="wp09b-dying")
        else:
            from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus

            transport = RabbitMqEventBus(url=broker_url, destinations=[destination], group="wp09b-dying")

        class DiesAfterThePublish:
            def subscribe(self, pattern, handler):
                transport.subscribe(pattern, handler)

            async def publish(self, destination, event_type, payload, headers=None):
                await transport.publish(destination, event_type, payload, headers)
                os._exit(137)  # the broker has the event; the process dies before the delivery is settled

            async def start(self):
                await transport.start()

            async def stop(self):
                await transport.stop()

        store = SqlOutboxStore(create_async_engine(database_url))
        forwarder = OutboxForwarder(
            store, DiesAfterThePublish(), name=broker, claim_timeout=10.0, handler_timeout=8.0
        )
        await forwarder.run_once()
        sys.exit(3)  # nothing was claimed


    asyncio.run(main(*sys.argv[1:]))
    """
)


async def test_a_forwarder_killed_between_the_broker_publish_and_the_settle_publishes_once_more_with_the_same_id(
    relational_backend: RelationalBackend, broker: Broker, tmp_path: Path
) -> None:
    engine = relational_backend.create_engine()
    publisher = TransactionalEventPublisher(broker.transport(), SqlOutboxStore(engine), name=broker.name)
    await publisher.store.start()
    await publisher.relay.register()
    async with TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine)).transaction():
        await publisher.publish(broker.destination, "order.placed", {"n": 1})
    (pending,) = await publisher.pending()

    script = tmp_path / "dying_forwarder.py"
    script.write_text(_DYING_FORWARDER)
    broker.queues.append(f"wp09b-dying.{broker.destination}")  # the dying process's bus declares it
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        relational_backend.url,
        broker.name,
        broker.url,
        broker.destination,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    assert process.returncode == 137, output.decode(errors="replace")[-2000:]
    first = await broker.messages(expected=1, settle=0.5)
    assert [message.payload for message in first] == [{"n": 1}]

    survivor = OutboxForwarder(
        publisher.store, broker.transport(), name=broker.name, claim_timeout=10.0, handler_timeout=8.0
    )
    try:
        assert await survivor.run_once() == 0  # the dead process's claim holds until its lease ends
        (claimed,) = await publisher.pending()
        assert claimed.attempts == 1
        deadline = time.monotonic() + 30
        while await survivor.run_once() == 0:
            assert time.monotonic() < deadline, "the dead process's lease never ended"
            await asyncio.sleep(0.5)
    finally:
        await survivor.transport.stop()

    messages = await broker.messages(expected=2)
    assert [message.payload for message in messages] == [{"n": 1}, {"n": 1}]  # at least once: once more, not more
    assert _event_ids(messages) == [pending.envelope.event_id] * 2  # a consumer deduplicates on it
    assert survivor.counters.delivered == 1
    assert await publisher.pending() == []


# ---------------------------------------------------------------------------------------------------------------
# A broker outage
# ---------------------------------------------------------------------------------------------------------------


class OutageProxy:
    """A TCP proxy in front of a broker, on a port of its own, that can be down (nothing listens there, every
    connection is closed) and up again on the same port: a broker that cannot be reached, then comes back."""

    def __init__(self, host: str, port: int) -> None:
        self._upstream = (host, port)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task[None]] = set()

    async def up(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", self.port)

    async def down(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        for writer in self._writers:
            writer.close()
        self._writers.clear()

    async def _accept(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        server_reader, server_writer = await asyncio.open_connection(*self._upstream)
        self._writers += [client_writer, server_writer]
        for source, target in ((client_reader, server_writer), (server_reader, client_writer)):
            task = asyncio.ensure_future(self._pipe(source, target))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError):
            while data := await source.read(65536):
                target.write(data)
                await target.drain()
        target.close()


def _behind(proxy: OutageProxy, broker: Broker) -> str:
    """The broker's URL through *proxy*."""
    if broker.name == "kafka":
        return f"127.0.0.1:{proxy.port}"  # the bootstrap server; the brokers it names are reached directly
    parts = urlsplit(broker.url)
    credentials = parts.netloc.rpartition("@")[0]
    return urlunsplit(parts._replace(netloc=f"{credentials}@127.0.0.1:{proxy.port}"))


def _upstream(broker: Broker) -> tuple[str, int]:
    if broker.name == "kafka":
        host, _, port = broker.url.split(",")[0].rpartition(":")
        return host, int(port)
    parts = urlsplit(broker.url)
    assert parts.hostname is not None and parts.port is not None
    return parts.hostname, parts.port


async def test_a_broker_outage_is_retried_then_dead_lettered_and_forwarding_resumes_after_it(
    relational_backend: RelationalBackend, broker: Broker
) -> None:
    engine = relational_backend.create_engine()
    proxy = OutageProxy(*_upstream(broker))  # down: nothing listens on its port yet
    publisher = TransactionalEventPublisher(
        broker.transport(_behind(proxy, broker)),
        SqlOutboxStore(engine),
        name=broker.name,
        retry=RetryPolicy(max_attempts=3, backoff=FixedBackOff(0.2)),
        handler_timeout=10.0,
        claim_timeout=30.0,
    )
    await publisher.store.start()
    await publisher.relay.register()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    try:
        async with template.transaction():
            await publisher.publish(broker.destination, "order.placed", {"n": "during the outage"})
        attempts: list[int] = []
        while not await publisher.dead_letters():
            if await publisher.relay.run_once():
                attempts.append(publisher.relay.counters.failures)
            assert len(attempts) <= 3, attempts
            await asyncio.sleep(0.25)

        assert attempts == [1, 2, 3]  # every attempt failed, and the third was the last
        (letter,) = await publisher.dead_letters()
        assert letter.event.payload == {"n": "during the outage"}
        assert letter.attempts == 3
        assert letter.error_type in ("KafkaConnectionError", "AMQPConnectionError"), letter.error_message
        assert await publisher.pending() == []

        await proxy.up()  # the broker is back
        async with template.transaction():
            await publisher.publish(broker.destination, "order.placed", {"n": "after the outage"})
        assert await publisher.relay.run_once() == 1
        assert publisher.relay.counters.delivered == 1
    finally:
        with contextlib.suppress(Exception):
            await publisher.transport.stop()
        await proxy.down()

    messages = await broker.messages(expected=1)
    assert [message.payload for message in messages] == [{"n": "after the outage"}]


# ---------------------------------------------------------------------------------------------------------------
# Two instances
# ---------------------------------------------------------------------------------------------------------------


async def test_two_instances_forward_each_event_once(relational_backend: RelationalBackend, broker: Broker) -> None:
    engine = relational_backend.create_engine()
    instances = [
        TransactionalEventPublisher(
            broker.transport(), SqlOutboxStore(engine), name=broker.name, poll_interval=0.1, batch_size=4
        )
        for _ in range(2)
    ]
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    for instance in instances:
        await instance.start()
    try:

        async def publish_from(instance: TransactionalEventPublisher, numbers: range) -> None:
            for number in numbers:
                async with template.transaction():
                    await instance.publish(broker.destination, "job.created", {"n": number})

        await asyncio.gather(publish_from(instances[0], range(0, 15)), publish_from(instances[1], range(15, 30)))
        await eventually(lambda: sum(i.relay.counters.delivered for i in instances) == 30, timeout=60)
    finally:
        for instance in instances:
            await instance.stop()

    messages = await broker.messages(expected=30)
    assert sorted(message.payload["n"] for message in messages) == list(range(30))  # none twice, none lost
    assert len(set(_event_ids(messages))) == 30
    assert await instances[0].pending() == []


# ---------------------------------------------------------------------------------------------------------------
# In an application context
# ---------------------------------------------------------------------------------------------------------------


@service
class OrderDesk:
    def __init__(self, orders: BrokerOrderRepository, events: EventPublisher) -> None:
        self.orders = orders
        self.events = events
        self.destination = ""

    @transactional
    async def place(self, name: str, *, fail: bool = False) -> None:
        await self.orders.save(BrokerOrder(name=name))
        await self.events.publish(self.destination, "order.placed", {"n": name})
        if fail:
            raise RuntimeError("payment declined")


@service
class OrderEvents:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["order.*"])
    async def on_order(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


async def test_the_auto_configured_publisher_carries_a_committed_unit_through_the_broker_to_the_listeners(
    relational_backend: RelationalBackend, broker: Broker
) -> None:
    await relational_backend.create_tables(BrokerOrder, *OutboxTables.named().all())
    group = f"wp09b-app-{uuid.uuid4().hex[:8]}"
    broker.queues.append(f"{group}.{broker.destination}")
    url_key = "pyfly.eda.kafka.bootstrap-servers" if broker.name == "kafka" else "pyfly.eda.rabbitmq.url"
    config = relational_backend.config(
        {
            "pyfly.eda.provider": broker.name,
            url_key: broker.url,
            "pyfly.eda.destinations": broker.destination,
            "pyfly.eda.group": group,
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.2",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (RelationalAutoConfiguration, EdaAutoConfiguration, BrokerOrderRepository, OrderDesk, OrderEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        assert publisher.group == f"pyfly.forward:{broker.name}"
        desk = ctx.get_bean(OrderDesk)
        desk.destination = broker.destination
        received = ctx.get_bean(OrderEvents).received

        with pytest.raises(RuntimeError, match="payment declined"):
            await desk.place("rolled back", fail=True)
        await desk.place("committed")

        await eventually(lambda: len(received) >= 1, timeout=60)
        await asyncio.sleep(1.5)  # a copy too many would arrive meanwhile
        assert [envelope.payload for envelope in received] == [{"n": "committed"}]
        assert received[0].headers[EVENT_ID_HEADER]
        assert publisher.relay.counters.delivered == 1
    finally:
        await ctx.stop()
