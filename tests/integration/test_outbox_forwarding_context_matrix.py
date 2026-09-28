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
"""``pyfly.eda.outbox.enabled`` in a real application context (WP09b), on every relational lane.

The auto-configured publisher of a provider that is not an outbox (the in-process bus here; Kafka and RabbitMQ in
``test_outbox_forwarding_brokers.py``) becomes transactional: a ``@transactional`` method that publishes and rolls
back publishes nothing, one that commits reaches the ``@event_listener`` once, after the commit, and so do the
domain events an aggregate raises (``pyfly.eda.domain-events.destination``). A data test's rollback transaction
keeps what it publishes out of the context's forwarder, and delivers it through a relay round of its own.
SQLite file (foreign keys on), PostgreSQL, MySQL 8 and MariaDB 11.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, Table, func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import transactional
from pyfly.domain import AggregateRoot, DomainEvent
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.domain_events import AGGREGATE_TYPE_HEADER, EVENT_ID_HEADER
from pyfly.eda.outbox import OutboxTables
from pyfly.eda.outbox_forwarding import TransactionalEventPublisher
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.testing import data_slice
from tests.support.backend_matrix import RelationalBackend

OUTBOX = OutboxTables.named()


@dataclass(frozen=True)
class CrateDispatched(DomainEvent):
    crate_id: int = 0
    dock: str = ""


class Crate(Base, AggregateRoot[int]):
    __tablename__ = "wp09b_crate"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))

    def dispatch(self, dock: str) -> None:
        self.status = "DISPATCHED"
        self.raise_event(CrateDispatched(crate_id=self.id, dock=dock))


@repository
class CrateRepository(Repository[Crate, int]):
    pass


@service
class Dispatch:
    def __init__(self, crates: CrateRepository, events: EventPublisher) -> None:
        self.crates = crates
        self.events = events

    @transactional
    async def dispatch(self, crate_id: int, dock: str, *, fail: bool = False) -> None:
        crate = await self.crates.find_by_id(crate_id)
        assert crate is not None
        crate.dispatch(dock)
        await self.crates.save(crate)
        await self.events.publish("warehouse", "crate.labelled", {"crate_id": crate_id, "dock": dock})
        if fail:
            raise RuntimeError("the forklift broke down")


@service
class Listeners:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    @event_listener(["CrateDispatched", "crate.*"])
    async def on_event(self, envelope: EventEnvelope) -> None:
        self.received.append(envelope)


def _config(backend: RelationalBackend, **overrides: Any) -> Any:
    return backend.config(
        {
            "pyfly.eda.provider": "memory",
            "pyfly.eda.outbox.enabled": "true",
            "pyfly.eda.outbox.poll-interval": "0.05",
            "pyfly.eda.domain-events.destination": "warehouse",
            **overrides,
        }
    )


async def _committed(backend: RelationalBackend, *tables: Table) -> dict[str, int]:
    """The rows another connection sees in each of *tables*: only what was committed."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return {
                table.name: int((await connection.execute(select(func.count()).select_from(table))).scalar() or 0)
                for table in tables
            }
    finally:
        await engine.dispose()


async def _eventually(condition: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_what_a_committed_unit_published_reaches_the_listeners_once_and_a_rolled_back_one_nothing(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(Crate, *OUTBOX.all())  # ddl-auto none: as migrations would
    ctx = ApplicationContext(_config(relational_backend))
    for bean in (RelationalAutoConfiguration, EdaAutoConfiguration, CrateRepository, Dispatch, Listeners):
        ctx.register_bean(bean)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        assert isinstance(publisher, TransactionalEventPublisher)
        assert isinstance(publisher.transport, InMemoryEventBus)
        received = ctx.get_bean(Listeners).received
        crate = await ctx.get_bean(CrateRepository).save(Crate(status="PACKED"))

        with pytest.raises(RuntimeError, match="forklift"):
            await ctx.get_bean(Dispatch).dispatch(crate.id, "dock-1", fail=True)
        await asyncio.sleep(0.3)  # several polls of the forwarder
        assert received == []
        assert await publisher.pending() == []

        await ctx.get_bean(Dispatch).dispatch(crate.id, "dock-2")
        await _eventually(lambda: len(received) == 2)
        await asyncio.sleep(0.3)
        assert sorted(envelope.event_type for envelope in received) == ["CrateDispatched", "crate.labelled"]
        dispatched = next(envelope for envelope in received if envelope.event_type == "CrateDispatched")
        assert dispatched.payload["dock"] == "dock-2"
        assert dispatched.headers[AGGREGATE_TYPE_HEADER] == "Crate"
        assert dispatched.headers[EVENT_ID_HEADER]  # the domain event's own id
        assert await publisher.pending() == []
        health = await publisher.health_status()
        assert health.status == "UP", health.details
        assert health.details["forwarded"] == 2
    finally:
        await ctx.stop()
    assert not publisher.forwarder.alive


async def test_a_data_test_forwards_what_it_published_with_a_relay_round_of_its_own_and_keeps_nothing(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(Crate, *OUTBOX.all())
    config = _config(relational_backend)
    async with await data_slice(CrateRepository, Dispatch, Listeners, config=config, rollback=True) as context:
        publisher = context.get_bean(EventPublisher)
        received = context.get_bean(Listeners).received
        crate = await context.get_bean(CrateRepository).save(Crate(status="PACKED"))
        await context.get_bean(Dispatch).dispatch(crate.id, "dock-3")  # the commit wakes the context's forwarder
        await asyncio.sleep(0.3)
        assert publisher.relay.alive  # type: ignore[attr-defined]
        assert received == []  # the forwarder the context started does not see what the test wrote
        assert await publisher.relay.run_once() == 2  # type: ignore[attr-defined]
        assert sorted(envelope.event_type for envelope in received) == ["CrateDispatched", "crate.labelled"]
        assert (await _committed(relational_backend, OUTBOX.events))[OUTBOX.events.name] == 0
    assert await _committed(relational_backend, OUTBOX.events, OUTBOX.deliveries, Crate.__table__) == {
        OUTBOX.events.name: 0,
        OUTBOX.deliveries.name: 0,
        Crate.__tablename__: 0,
    }
