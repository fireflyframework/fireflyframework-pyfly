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
"""The relay, the ``database`` bus and the event-sourcing ``TransactionalOutbox`` depend on the outbox store port
alone (WP09b).

Each runs here on a :class:`~tests.support.outbox_contract.PortOnlyStore`: a real SQL store behind a wrapper that
has the port's methods and nothing else. What they did through the SQL class's own methods (its engine, its
dialect, its datasource) would fail on it; a document store is such a store. SQLite file (foreign keys on),
PostgreSQL, MySQL 8 and MariaDB 11.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eda.adapters.database import DatabaseEventBus, ListenerState
from pyfly.eda.outbox import OutboxRelay, SqlOutboxStore
from pyfly.eda.types import EventEnvelope
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.outbox import TransactionalOutbox
from pyfly.messaging.listener_container import FixedBackOff, RetryPolicy
from tests.support.backend_matrix import PG, RelationalBackend
from tests.support.outbox_contract import PortOnlyStore


async def _store(backend: RelationalBackend) -> tuple[PortOnlyStore, TransactionTemplate]:
    engine = backend.create_engine()
    store = PortOnlyStore(SqlOutboxStore(engine))
    await store.start()
    return store, TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))


async def test_the_database_bus_runs_on_any_outbox_store(relational_backend: RelationalBackend) -> None:
    store, template = await _store(relational_backend)
    bus = DatabaseEventBus(store=store, group="billing", retry=RetryPolicy(max_attempts=1, backoff=FixedBackOff(0)))
    seen: list[Any] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope.payload["n"])

    bus.subscribe("order.*", handler)
    await bus.start()
    try:
        assert bus.outbox is store
        assert bus.sql_store is None
        assert bus.listener_state is ListenerState.OFF  # the PostgreSQL wake-ups are the SQL store's
        with pytest.raises(RuntimeError, match="declined"):
            async with template.transaction():
                await bus.publish("orders", "order.placed", {"n": 1})
                raise RuntimeError("declined")
        async with template.transaction():
            await bus.publish("orders", "order.placed", {"n": 2})
        for _ in range(500):  # the commit wakes the running relay, which settles the delivery once handled
            if bus.relay.counters.delivered:
                break
            await asyncio.sleep(0.01)
        assert seen == [2]
        health = await bus.health_status()
        assert health.status == "UP", health.details
        assert health.details["delivered"] == 1
    finally:
        await bus.stop()


@pytest.mark.backends(PG)
async def test_the_database_bus_on_another_store_refuses_the_postgresql_wake_ups(
    relational_backend: RelationalBackend,
) -> None:
    store, _template = await _store(relational_backend)
    bus = DatabaseEventBus(store=store, notify=True)
    with pytest.raises(ValueError, match="SQL outbox store on PostgreSQL"):
        await bus.start()
    with pytest.raises(ValueError, match="not both"):
        DatabaseEventBus("primary", store=store)


async def test_the_relay_runs_on_any_outbox_store(relational_backend: RelationalBackend) -> None:
    store, template = await _store(relational_backend)
    relay = OutboxRelay(store, group="audit", transactional=False)
    seen: list[Any] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope.payload["n"])

    relay.subscribe("*", handler)
    await relay.register()
    async with template.transaction():
        await store.append(EventEnvelope("login", {"n": "a"}, "audit"))
    assert await relay.run_once() == 1
    assert seen == ["a"]
    assert relay.outbox is store


async def test_the_event_sourcing_outbox_runs_on_any_outbox_store(relational_backend: RelationalBackend) -> None:
    store, template = await _store(relational_backend)
    published: list[StoredEventEnvelope] = []

    async def publish(envelope: StoredEventEnvelope) -> None:
        published.append(envelope)

    outbox = TransactionalOutbox(publish, store=store, backoff=FixedBackOff(0.0))
    event = StoredEventEnvelope(aggregate_id="a-1", aggregate_type="Account", sequence=1, event_type="Opened")
    with pytest.raises(RuntimeError, match="rolled back"):
        async with template.transaction():
            await outbox.enqueue(event)
            raise RuntimeError("rolled back")
    assert await outbox.pending() == []

    async with template.transaction():
        await outbox.enqueue(event)
    assert [record.event.aggregate_id for record in await outbox.pending()] == ["a-1"]
    assert await outbox.relay.run_once() == 1
    assert [envelope.aggregate_id for envelope in published] == ["a-1"]
    assert outbox.outbox is store
    with pytest.raises(ValueError, match="not both"):
        TransactionalOutbox(publish, datasource="primary", store=store)
