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
"""Domain events of MongoDB aggregates, published as their unit of work commits (a real replica set).

The ``DomainEventPublisher`` collected the events of relational aggregates only (its save hook listens to the
SQLAlchemy session). An :class:`~pyfly.data.document.mongodb.document.AggregateDocument` raises events like an
``AggregateRoot``: those raised inside a document unit are tied to it, and those pending on a document the
repository saves are tied to the unit of the save. They are published when that unit commits, and not at all
when it rolls back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from beanie import Document

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.data.document.mongodb.document import AggregateDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import TransactionPhase, TransactionTemplate
from pyfly.domain import DomainEvent
from pyfly.eda.domain_events import DomainEventPublisher
from tests.support.mongo import BeanieDatabase, beanie_database


@dataclass(frozen=True)
class OrderConfirmed(DomainEvent):
    order: str = ""


class EvOrder(AggregateDocument):
    reference: str
    status: str = "NEW"

    class Settings:
        name = "ev_orders"

    def confirm(self) -> None:
        self.status = "CONFIRMED"
        self.raise_event(OrderConfirmed(order=self.reference))


@dataclass
class Env:
    db: BeanieDatabase
    template: TransactionTemplate
    orders: MongoRepository[EvOrder, str]
    seen: list[tuple[str, str]]


@pytest.fixture
async def env(mongo_rs_url: str) -> AsyncIterator[Env]:
    bus = ApplicationEventBus()
    seen: list[tuple[str, str]] = []

    async def inline(event: OrderConfirmed) -> None:
        seen.append(("inline", event.order))

    async def after_commit(event: OrderConfirmed) -> None:
        seen.append(("after_commit", event.order))

    bus.subscribe(OrderConfirmed, inline)
    bus.subscribe(OrderConfirmed, after_commit, phase=TransactionPhase.AFTER_COMMIT)
    publisher = DomainEventPublisher(ApplicationEventPublisher(bus))
    await publisher.start()
    try:
        async with beanie_database(mongo_rs_url, [EvOrder]) as db:
            template = TransactionTemplate(MongoTransactionManager.for_client(db.client))
            yield Env(db, template, MongoRepository(EvOrder), seen)
    finally:
        await publisher.stop()


async def _statuses(env: Env) -> dict[str, Any]:
    return {row["reference"]: row["status"] for row in await env.db.database["ev_orders"].find({}).to_list()}


async def test_events_raised_in_a_document_unit_are_published_as_it_commits(env: Env) -> None:
    order = EvOrder(reference="o-1")
    async with env.template.transaction():
        order.confirm()
        await env.orders.save(order)
        assert env.seen == []  # nothing before the commit
    assert env.seen == [("inline", "o-1"), ("after_commit", "o-1")]
    assert order.pending_events() == []
    assert await _statuses(env) == {"o-1": "CONFIRMED"}


async def test_a_rolled_back_unit_publishes_nothing_and_keeps_the_events_pending(env: Env) -> None:
    order = EvOrder(reference="o-2")
    with pytest.raises(RuntimeError):
        async with env.template.transaction():
            order.confirm()
            await env.orders.save(order)
            raise RuntimeError("rolled back")
    assert env.seen == []
    assert len(order.pending_events()) == 1
    assert await _statuses(env) == {}
    await env.orders.save(order)  # saved again, in a unit of its own: published as that one commits
    assert env.seen == [("inline", "o-2"), ("after_commit", "o-2")]
    assert order.pending_events() == []


async def test_the_pending_events_of_a_saved_aggregate_are_published_with_the_save(env: Env) -> None:
    orders = [EvOrder(reference=f"b-{index}") for index in range(3)]
    for order in orders:
        order.confirm()  # raised outside any unit: pending until a unit saves the aggregate
    assert env.seen == []
    await env.orders.save_all(orders)
    assert sorted(order for phase, order in env.seen if phase == "after_commit") == ["b-0", "b-1", "b-2"]
    assert all(order.pending_events() == [] for order in orders)


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order: str = ""


class EvShipment(Document):
    order: str

    class Settings:
        name = "ev_shipments"


async def test_a_before_commit_listener_that_makes_the_units_first_write_runs_once(mongo_rs_url: str) -> None:
    """The unit's body writes nothing through a repository; the before-commit listener of the event its aggregate
    raises saves a projection, the unit's first repository write. The listener runs once, and one projection is
    stored."""
    bus = ApplicationEventBus()
    calls: list[str] = []
    shipments: MongoRepository[EvShipment, str] = MongoRepository(EvShipment)

    async def project(event: OrderShipped) -> None:
        calls.append(event.order)
        await shipments.save(EvShipment(order=event.order))

    bus.subscribe(OrderShipped, project, phase=TransactionPhase.BEFORE_COMMIT)
    publisher = DomainEventPublisher(ApplicationEventPublisher(bus))
    await publisher.start()
    try:
        async with beanie_database(mongo_rs_url, [EvOrder, EvShipment]) as db:
            order = await MongoRepository(EvOrder).save(EvOrder(reference="o-9"))
            async with TransactionTemplate(MongoTransactionManager.for_client(db.client)).transaction():
                order.raise_event(OrderShipped(order="o-9"))
            assert calls == ["o-9"]
            assert await db.database["ev_shipments"].count_documents({}) == 1
    finally:
        await publisher.stop()
