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
"""A document-only application's transactional outbox on MongoDB, auto-configured, on a real replica set.

With ``pyfly.data.document.enabled`` and no relational datasource, ``pyfly.eda.outbox.store=auto`` picks the Mongo
store: the in-process transport made transactional (``pyfly.eda.outbox.enabled``) and the ``database`` bus keep
their outbox in the document database, on the document datasource's units of work. A ``@transactional`` method
that saves a document and publishes an event commits both or neither: a rolled-back one reaches no listener, a
committed one reaches it once. The events a MongoDB aggregate raises (``pyfly.eda.domain-events.destination``) are
appended in the unit that saves it, as an outbox bus appends a relational aggregate's; saved outside a transaction,
each with its deliveries in a transaction of the store's own, so they do not commit together. (The Kafka and
RabbitMQ lanes are ``test_mongo_outbox_forwarding_brokers.py``.)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest
from beanie import Document, PydanticObjectId

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.document.mongodb.document import AggregateDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import transactional
from pyfly.domain import DomainEvent
from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.exceptions import DuplicateKeyException
from tests.support.backend_matrix import MongoBackend
from tests.support.brokers import eventually

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.mongo]


class AppOrder(Document):
    name: str

    class Settings:
        name = "wp06b_app_orders"


@repository
class AppOrderRepository(MongoRepository[AppOrder, PydanticObjectId]):
    pass


@service
class AppOrderDesk:
    def __init__(self, orders: AppOrderRepository, events: EventPublisher) -> None:
        self.orders = orders
        self.events = events

    @transactional
    async def place(self, name: str, *, fail: bool = False) -> None:
        await self.orders.save(AppOrder(name=name))
        await self.events.publish("pyfly.events", "order.placed", {"n": name})
        if fail:
            raise RuntimeError("payment declined")


@service
class AppOrderEvents:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["order.*"])
    async def on_order(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


@dataclass(frozen=True)
class AppOrderShipped(DomainEvent):
    order: str = ""


@dataclass(frozen=True)
class AppOrderBilled(DomainEvent):
    order: str = ""


class ShippedOrder(AggregateDocument):
    reference: str

    class Settings:
        name = "wp06b_shipped_orders"

    def ship(self) -> None:
        self.raise_event(AppOrderShipped(order=self.reference))

    def bill(self) -> None:
        self.raise_event(AppOrderBilled(order=self.reference))


@repository
class ShippedOrderRepository(MongoRepository[ShippedOrder, PydanticObjectId]):
    pass


@service
class Shipping:
    def __init__(self, orders: ShippedOrderRepository) -> None:
        self.orders = orders

    @transactional
    async def ship(self, reference: str, *, fail: bool = False) -> None:
        order = ShippedOrder(reference=reference)
        order.ship()
        await self.orders.save(order)
        if fail:
            raise RuntimeError("carrier unavailable")


@service
class ShippedEvents:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["AppOrderShipped"])
    async def on_shipped(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


@service
class ShippedAndBilledEvents:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["AppOrderShipped", "AppOrderBilled"])
    async def on_order(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


@pytest.mark.parametrize("provider", ["memory", "database"])
async def test_a_document_only_application_keeps_its_outbox_in_mongodb(
    mongo_backend: MongoBackend, provider: str
) -> None:
    config = mongo_backend.config(
        {
            "pyfly.eda.provider": provider,
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.2",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (EdaAutoConfiguration, AppOrderRepository, AppOrderDesk, AppOrderEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        if provider == "memory":
            assert isinstance(publisher, TransactionalEventPublisher)
            store = publisher.store
        else:
            assert type(publisher) is DatabaseEventBus
            store = publisher.outbox
        assert isinstance(store, MongoOutboxStore)
        assert store.manager() is ctx.get_bean(MongoTransactionManager)  # the document datasource's units
        assert store.database == mongo_backend.database
        desk = ctx.get_bean(AppOrderDesk)
        received = ctx.get_bean(AppOrderEvents).received

        with pytest.raises(RuntimeError, match="payment declined"):
            await desk.place("rolled back", fail=True)
        await desk.place("committed")

        await eventually(lambda: len(received) >= 1)
        await asyncio.sleep(1.0)  # a copy too many would arrive meanwhile
        assert [envelope.payload for envelope in received] == [{"n": "committed"}]
        if provider == "memory":
            assert received[0].headers[EVENT_ID_HEADER]
        assert publisher.relay.counters.delivered == 1  # type: ignore[attr-defined]
        database = store.client[mongo_backend.database]
        assert await database["wp06b_app_orders"].count_documents({}) == 1
        assert await database[store.collections.events].count_documents({}) == 1
        assert await store.pending(publisher.relay.group) == []  # type: ignore[attr-defined]  # settled once handled
    finally:
        await ctx.stop()


async def test_the_events_of_a_mongodb_aggregate_go_through_the_outbox_in_the_unit_that_saves_it(
    mongo_backend: MongoBackend,
) -> None:
    config = mongo_backend.config(
        {
            "pyfly.eda.provider": "memory",
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.2",
            "pyfly.eda.domain-events.destination": "shipping",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (EdaAutoConfiguration, ShippedOrderRepository, Shipping, ShippedEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        assert isinstance(publisher.store, MongoOutboxStore)
        shipping = ctx.get_bean(Shipping)
        received = ctx.get_bean(ShippedEvents).received

        with pytest.raises(RuntimeError, match="carrier unavailable"):
            await shipping.ship("s-1", fail=True)
        assert await publisher.pending() == []  # the rolled-back unit appended nothing
        await shipping.ship("s-2")

        await eventually(lambda: len(received) >= 1)
        await asyncio.sleep(1.0)  # a copy too many would arrive meanwhile
        assert [(envelope.destination, envelope.payload["order"]) for envelope in received] == [("shipping", "s-2")]
        assert received[0].headers[EVENT_ID_HEADER] == received[0].payload["event_id"]  # the domain event's id
        database = publisher.store.client[mongo_backend.database]
        assert await database["wp06b_shipped_orders"].count_documents({}) == 1
    finally:
        await ctx.stop()


async def test_an_event_of_an_aggregate_saved_outside_a_transaction_is_written_with_its_deliveries_or_not_at_all(
    mongo_backend: MongoBackend,
) -> None:
    """``MongoRepository.save`` outside ``@transactional`` writes the document in a unit that runs no transaction,
    and appends the aggregate's events as that unit commits: in a transaction of the store's own, so an event is
    written with its deliveries or not at all (never left owed to no group, where retention would drop it)."""
    config = mongo_backend.config(
        {
            "pyfly.eda.provider": "memory",
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.2",
            "pyfly.eda.domain-events.destination": "shipping",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (EdaAutoConfiguration, ShippedOrderRepository, ShippedEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        store = publisher.store
        assert isinstance(store, MongoOutboxStore)
        orders = ctx.get_bean(ShippedOrderRepository)
        received = ctx.get_bean(ShippedEvents).received
        database = store.client[mongo_backend.database]

        order = ShippedOrder(reference="s-1")
        order.ship()
        await orders.save(order)
        await eventually(lambda: len(received) >= 1)

        counter = await database[store.collections.counters].find_one({"_id": store.collections.events})
        assert counter is not None
        doomed = int(counter["value"]) + 1
        # The next event's delivery to the relay's group clashes with this one (not due for a day, so the relay
        # leaves it alone) on the unique (consumer_group, outbox_id) index.
        clash = {"consumer_group": publisher.relay.group, "outbox_id": doomed}
        await database[store.collections.deliveries].insert_one(
            {**clash, "available_at": store.now() + timedelta(days=1), "attempts": 0}
        )
        failed = ShippedOrder(reference="s-2")
        failed.ship()
        with pytest.raises(DuplicateKeyException):  # the repository's translation of the append's failure
            await orders.save(failed)
        assert await database[store.collections.events].find_one({"_id": doomed}) is None
        await database[store.collections.deliveries].delete_one(clash)

        await asyncio.sleep(1.0)  # a copy too many would arrive meanwhile
        assert [(envelope.destination, envelope.payload["order"]) for envelope in received] == [("shipping", "s-1")]
        assert await database[store.collections.events].count_documents({}) == 1
        assert await store.pending(publisher.relay.group) == []
    finally:
        await ctx.stop()


async def test_each_event_of_an_aggregate_saved_outside_a_transaction_is_a_transaction_of_its_own(
    mongo_backend: MongoBackend,
) -> None:
    """Outside ``@transactional`` the events of one ``MongoRepository.save`` are appended one by one, each with its
    deliveries in a transaction of the store's own: they do not commit together. When the second one fails, the
    first stands and is delivered (once), the second is not written at all, and the save raises with its document
    stored. (A ``@transactional`` boundary is what makes an aggregate's events commit together.)"""
    config = mongo_backend.config(
        {
            "pyfly.eda.provider": "memory",
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.2",
            "pyfly.eda.domain-events.destination": "shipping",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (EdaAutoConfiguration, ShippedOrderRepository, ShippedAndBilledEvents):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        store = publisher.store
        assert isinstance(store, MongoOutboxStore)
        orders = ctx.get_bean(ShippedOrderRepository)
        received = ctx.get_bean(ShippedAndBilledEvents).received
        database = store.client[mongo_backend.database]

        counter = await database[store.collections.counters].find_one({"_id": store.collections.events})
        assert counter is not None
        shipped, billed = int(counter["value"]) + 1, int(counter["value"]) + 2  # the ids the two appends take
        # The second event's delivery to the relay's group clashes with this one (not due for a day, so the relay
        # leaves it alone) on the unique (consumer_group, outbox_id) index.
        clash = {"consumer_group": publisher.relay.group, "outbox_id": billed}
        await database[store.collections.deliveries].insert_one(
            {**clash, "available_at": store.now() + timedelta(days=1), "attempts": 0}
        )
        order = ShippedOrder(reference="p-1")
        order.ship()
        order.bill()
        with pytest.raises(DuplicateKeyException):  # the repository's translation of the second append's failure
            await orders.save(order)

        assert await database["wp06b_shipped_orders"].count_documents({"reference": "p-1"}) == 1  # the save's own
        first = await database[store.collections.events].find_one({"_id": shipped})
        assert first is not None and first["event_type"] == "AppOrderShipped"  # committed on its own
        assert await database[store.collections.events].find_one({"_id": billed}) is None  # not written at all
        await database[store.collections.deliveries].delete_one(clash)

        await eventually(lambda: len(received) >= 1)
        await asyncio.sleep(1.0)  # a copy too many, or the second event, would arrive meanwhile
        assert [(envelope.event_type, envelope.payload["order"]) for envelope in received] == [
            ("AppOrderShipped", "p-1")
        ]
        assert await database[store.collections.events].count_documents({}) == 1
        assert await store.pending(publisher.relay.group) == []
    finally:
        await ctx.stop()
