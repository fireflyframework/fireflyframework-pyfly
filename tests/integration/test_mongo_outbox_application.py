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
committed one reaches it once. (The Kafka and RabbitMQ lanes are ``test_mongo_outbox_forwarding_brokers.py``.)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from beanie import Document, PydanticObjectId

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import transactional
from pyfly.eda.adapters.database import DatabaseEventBus
from pyfly.eda.adapters.mongo_outbox import MongoOutboxStore
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.domain_events import EVENT_ID_HEADER
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from tests.support.backend_matrix import MongoBackend

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


async def _eventually(condition: Any, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


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

        await _eventually(lambda: len(received) >= 1)
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
