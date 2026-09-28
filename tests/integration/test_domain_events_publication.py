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
"""Domain events are published when the unit of work that persists their aggregate commits (C144).

``AggregateRoot.raise_event`` promised publication "after the unit of work commits", and nothing published
anything; the docs told the application to publish before committing, so a rollback left phantom events
behind. A real ``ApplicationContext`` (relational and EDA auto-configuration, the ``database`` outbox bus)
on every relational lane (SQLite file with foreign keys on, PostgreSQL, MySQL, MariaDB):

- the events an aggregate raises inside a ``@transactional`` method reach the ``AFTER_COMMIT`` listeners
  once it committed, and the outbox in the same unit (an ``@event_listener`` then receives them);
- a unit that rolls back publishes nothing, to either;
- the events an aggregate raised before any unit existed are published by the unit that saves it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import app_event_listener
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import TransactionPhase, transactional
from pyfly.domain import AggregateRoot, DomainEvent
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener
from pyfly.eda.outbox import OutboxTables
from pyfly.eda.types import EventEnvelope
from tests.support.backend_matrix import RelationalBackend


@dataclass(frozen=True)
class ParcelShipped(DomainEvent):
    parcel_id: int = 0
    carrier: str = ""


class Parcel(Base, AggregateRoot[int]):
    __tablename__ = "wp09_parcel"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))

    @classmethod
    def register(cls, status: str) -> Parcel:
        parcel = cls(status=status)
        parcel.raise_event(ParcelShipped(parcel_id=0, carrier="registered"))  # before any unit exists
        return parcel

    def ship(self, carrier: str) -> None:
        self.status = "SHIPPED"
        self.raise_event(ParcelShipped(parcel_id=self.id, carrier=carrier))


@repository
class ParcelRepository(Repository[Parcel, int]):
    pass


@service
class Shipping:
    def __init__(self, parcels: ParcelRepository) -> None:
        self.parcels = parcels

    @transactional
    async def ship(self, parcel_id: int, carrier: str, *, fail: bool = False) -> None:
        parcel = await self.parcels.find_by_id(parcel_id)
        assert parcel is not None
        parcel.ship(carrier)
        await self.parcels.save(parcel)
        if fail:
            raise RuntimeError("the label printer jammed")


@service
class Listeners:
    def __init__(self) -> None:
        self.after_commit: list[tuple[int, str]] = []
        self.inline: list[str] = []
        self.eda: list[EventEnvelope] = []

    @app_event_listener
    async def seen_inline(self, event: ParcelShipped) -> None:
        self.inline.append(event.carrier)

    @app_event_listener(phase=TransactionPhase.AFTER_COMMIT)
    async def notify_customer(self, event: ParcelShipped) -> None:
        self.after_commit.append((event.parcel_id, event.carrier))

    @event_listener(["ParcelShipped"])
    async def on_the_bus(self, envelope: EventEnvelope) -> None:
        self.eda.append(envelope)


async def _boot(backend: RelationalBackend) -> ApplicationContext:
    await backend.create_tables(Parcel)
    await backend.create_tables(*OutboxTables.named().all())  # ddl-auto none: the outbox tables come from migrations
    config = backend.config(
        {
            "pyfly.data.relational.ddl-auto": "none",
            "pyfly.eda.provider": "database",
            "pyfly.eda.destinations": "shipping",
            "pyfly.eda.group": "shipping-app",
            "pyfly.eda.domain-events.destination": "shipping",
            "pyfly.eda.outbox.poll-interval": "0.1",
        }
    )
    ctx = ApplicationContext(config)
    for bean in (RelationalAutoConfiguration, EdaAutoConfiguration, ParcelRepository, Shipping, Listeners):
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


async def _outbox_rows(url: str) -> list[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT payload FROM pyfly_outbox_events ORDER BY id"))
            return [row[0] for row in rows.all()]
    finally:
        await engine.dispose()


async def _wait_for(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_events_raised_in_a_committed_unit_are_published_after_it(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        parcels = ctx.get_bean(ParcelRepository)
        parcel = await parcels.save(Parcel(status="NEW"))
        listeners = ctx.get_bean(Listeners)

        await ctx.get_bean(Shipping).ship(parcel.id, "courier")

        assert listeners.inline == ["courier"]
        assert listeners.after_commit == [(parcel.id, "courier")]
        await _wait_for(lambda: len(listeners.eda) == 1)
        envelope = listeners.eda[0]
        assert envelope.event_type == "ParcelShipped"
        assert envelope.payload["parcel_id"] == parcel.id
        assert envelope.payload["carrier"] == "courier"
        assert isinstance(envelope.payload["occurred_at"], str)  # ISO-8601, not a datetime the bus cannot carry
        assert envelope.headers["x-pyfly-aggregate-type"] == "Parcel"
        assert envelope.headers["x-pyfly-aggregate-id"] == str(parcel.id)
    finally:
        await ctx.stop()


async def test_a_unit_that_rolls_back_publishes_nothing(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        parcels = ctx.get_bean(ParcelRepository)
        parcel = await parcels.save(Parcel(status="NEW"))
        listeners = ctx.get_bean(Listeners)

        with pytest.raises(RuntimeError, match="label printer"):
            await ctx.get_bean(Shipping).ship(parcel.id, "courier", fail=True)

        assert listeners.after_commit == []
        assert await _outbox_rows(relational_backend.url) == []
        stored = await parcels.find_by_id(parcel.id)
        assert stored is not None and stored.status == "NEW"
    finally:
        await ctx.stop()


async def test_events_raised_before_any_unit_are_published_by_the_unit_that_saves_the_aggregate(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        listeners = ctx.get_bean(Listeners)
        parcel = Parcel.register("NEW")  # raised outside any unit of work: pending

        await ctx.get_bean(ParcelRepository).save(parcel)  # the save's own unit commits it, and its event

        assert listeners.after_commit == [(0, "registered")]
        assert parcel.pending_events() == []
        await _wait_for(lambda: len(listeners.eda) == 1)
    finally:
        await ctx.stop()
