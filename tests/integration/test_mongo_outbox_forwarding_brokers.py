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
"""Kafka and RabbitMQ made transactional by the outbox layer on MongoDB: a real broker and a real replica set.

The forwarding layer (``TransactionalEventPublisher`` and its ``OutboxForwarder``) runs unchanged on
:class:`~pyfly.eda.adapters.mongo_outbox.MongoOutboxStore`. Read back from the broker itself:

- a Mongo unit of work that saves a document and publishes, then rolls back, publishes nothing; one that commits
  publishes exactly one message, with the event's id in ``x-pyfly-event-id``;
- a document-only application (``pyfly.data.document.enabled``, ``pyfly.eda.outbox.enabled``, no relational
  datasource) carries a committed ``@transactional`` method's event through the broker to its
  ``@event_listener`` once, and a rolled-back one's never.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from beanie import Document, PydanticObjectId

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import TransactionTemplate, transactional
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from tests.support.backend_matrix import MongoBackend
from tests.support.brokers import Broker, eventually
from tests.support.mongo import beanie_database

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.brokers, pytest.mark.mongo]


@pytest.fixture(params=["kafka", "rabbitmq"])
async def broker(request: pytest.FixtureRequest) -> AsyncIterator[Broker]:
    """A lane of ``test_outbox_forwarding_brokers.py``: the Kafka or the RabbitMQ testcontainer, read back directly."""
    url: str = request.getfixturevalue("kafka_url" if request.param == "kafka" else "amqp_url")
    lane = Broker(request.param, url)
    await lane.listen()
    try:
        yield lane
    finally:
        await lane.cleanup()


class ForwardedDoc(Document):
    name: str

    class Settings:
        name = "wp06b_forwarded_orders"


async def test_a_mongo_unit_that_rolls_back_publishes_nothing_and_one_that_commits_publishes_once(
    mongo_rs_url: str, broker: Broker
) -> None:
    async with beanie_database(mongo_rs_url, [ForwardedDoc]) as db:
        manager = MongoTransactionManager.for_client(db.client)
        store = MongoOutboxStore(manager, database=db.name)
        publisher = TransactionalEventPublisher(broker.transport(), store, name=broker.name, poll_interval=0.2)
        orders: MongoRepository[ForwardedDoc, PydanticObjectId] = MongoRepository(ForwardedDoc)
        template = TransactionTemplate(manager)
        await publisher.start()
        try:
            with pytest.raises(RuntimeError, match="payment declined"):
                async with template.transaction():
                    await orders.save(ForwardedDoc(name="rolled back"))
                    await publisher.publish(broker.destination, "order.placed", {"n": "rolled back"})
                    raise RuntimeError("payment declined")
            async with template.transaction():
                await orders.save(ForwardedDoc(name="committed"))
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
        assert [doc.name for doc in await orders.find_all()] == ["committed"]
        assert await publisher.pending() == []


class DeskOrder(Document):
    name: str

    class Settings:
        name = "wp06b_desk_orders"


@repository
class DeskOrderRepository(MongoRepository[DeskOrder, PydanticObjectId]):
    pass


@service
class MongoOrderDesk:
    def __init__(self, orders: DeskOrderRepository, events: EventPublisher) -> None:
        self.orders = orders
        self.events = events
        self.destination = ""

    @transactional
    async def place(self, name: str, *, fail: bool = False) -> None:
        await self.orders.save(DeskOrder(name=name))
        await self.events.publish(self.destination, "order.placed", {"n": name})
        if fail:
            raise RuntimeError("payment declined")


@service
class MongoOrderEvents:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["order.*"])
    async def on_order(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


async def test_a_document_only_application_carries_a_committed_unit_through_the_broker_to_the_listeners(
    mongo_backend: MongoBackend, broker: Broker
) -> None:
    group = f"wp06b-app-{uuid.uuid4().hex[:8]}"
    broker.queues.append(f"{group}.{broker.destination}")
    url_key = "pyfly.eda.kafka.bootstrap-servers" if broker.name == "kafka" else "pyfly.eda.rabbitmq.url"
    config = mongo_backend.config(
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
    for bean in (EdaAutoConfiguration, DeskOrderRepository, MongoOrderDesk, MongoOrderEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        assert isinstance(publisher.store, MongoOutboxStore)
        assert publisher.group == f"pyfly.forward:{broker.name}"
        desk = ctx.get_bean(MongoOrderDesk)
        desk.destination = broker.destination
        received = ctx.get_bean(MongoOrderEvents).received

        with pytest.raises(RuntimeError, match="payment declined"):
            await desk.place("rolled back", fail=True)
        await desk.place("committed")

        await eventually(lambda: len(received) >= 1, timeout=60)
        await asyncio.sleep(1.5)  # a copy too many would arrive meanwhile
        assert [envelope.payload for envelope in received] == [{"n": "committed"}]
        assert received[0].headers[EVENT_ID_HEADER]
        assert publisher.relay.counters.delivered == 1
        assert [order.name for order in await ctx.get_bean(DeskOrderRepository).find_all()] == ["committed"]
    finally:
        await ctx.stop()
