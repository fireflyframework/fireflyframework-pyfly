# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""End-to-end tests for the event-sourcing module."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from pyfly.eventsourcing.aggregate import AggregateRoot
from pyfly.eventsourcing.event import DomainEvent, StoredEventEnvelope
from pyfly.eventsourcing.outbox import TransactionalOutbox
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner
from pyfly.eventsourcing.repository import EventSourcedRepository
from pyfly.eventsourcing.snapshot import InMemorySnapshotStore
from pyfly.eventsourcing.store import ConcurrencyError, InMemoryEventStore


@dataclass
class OrderPlaced(DomainEvent):
    order_id: str = ""
    amount: int = 0


@dataclass
class OrderShipped(DomainEvent):
    order_id: str = ""
    carrier: str = ""


class Order(AggregateRoot):
    def __init__(self) -> None:
        super().__init__()
        self.amount = 0
        self.shipped = False
        self.when(OrderPlaced, lambda agg, evt: setattr(agg, "amount", evt.amount))
        self.when(OrderShipped, lambda agg, evt: setattr(agg, "shipped", True))


class TestInMemoryEventStore:
    @pytest.mark.asyncio
    async def test_append_and_load(self) -> None:
        store = InMemoryEventStore()
        envelopes = [
            StoredEventEnvelope.of("o-1", "Order", 0, OrderPlaced(order_id="o-1", amount=42)),
        ]
        await store.append("o-1", "Order", envelopes, expected_version=0)
        loaded = await store.load("o-1")
        assert len(loaded) == 1
        assert loaded[0].sequence == 1

    @pytest.mark.asyncio
    async def test_concurrency_error(self) -> None:
        store = InMemoryEventStore()
        envelopes = [StoredEventEnvelope.of("o", "Order", 0, OrderPlaced())]
        await store.append("o", "Order", envelopes, expected_version=0)
        with pytest.raises(ConcurrencyError):
            await store.append("o", "Order", envelopes, expected_version=0)

    @pytest.mark.asyncio
    async def test_stream_all(self) -> None:
        store = InMemoryEventStore()
        for i in range(3):
            await store.append(
                f"o-{i}",
                "Order",
                [StoredEventEnvelope.of(f"o-{i}", "Order", 0, OrderPlaced())],
                expected_version=0,
            )
        all_events = await store.stream_all()
        assert len(all_events) == 3


class TestRepository:
    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self) -> None:
        store = InMemoryEventStore()
        repo: EventSourcedRepository[Order] = EventSourcedRepository(store=store, factory=Order)
        order = Order()
        order.id = "o-1"
        order.apply(OrderPlaced(order_id="o-1", amount=99))
        order.apply(OrderShipped(order_id="o-1", carrier="ups"))
        await repo.save(order)

        # Reload from event store.
        reloaded = await repo.load("o-1")
        assert reloaded is not None
        assert reloaded.amount == 99
        assert reloaded.shipped is True
        assert reloaded.version == 2


class TestSnapshotStore:
    @pytest.mark.asyncio
    async def test_snapshot_save_and_load(self) -> None:
        from pyfly.eventsourcing.snapshot import Snapshot

        store = InMemorySnapshotStore()
        snap = Snapshot(aggregate_id="o", aggregate_type="Order", sequence=10, payload={"x": 1})
        await store.save(snap)
        loaded = await store.load("o")
        assert loaded is not None and loaded.sequence == 10


class TestOutbox:
    @pytest.mark.asyncio
    async def test_outbox_publishes(self) -> None:
        published: list[StoredEventEnvelope] = []

        async def publish(env: StoredEventEnvelope) -> None:
            published.append(env)

        outbox = TransactionalOutbox(publish=publish, poll_interval_s=0.05)
        record = await outbox.enqueue(StoredEventEnvelope.of("o", "Order", 0, OrderPlaced()))
        await outbox.start()
        await asyncio.sleep(0.2)
        await outbox.stop()
        assert record.delivered
        assert len(published) == 1


class TestProjection:
    @pytest.mark.asyncio
    async def test_projection_consumes_events(self) -> None:
        store = InMemoryEventStore()
        for i in range(3):
            await store.append(
                f"o-{i}",
                "Order",
                [StoredEventEnvelope.of(f"o-{i}", "Order", 0, OrderPlaced())],
                expected_version=0,
            )
        seen: list[StoredEventEnvelope] = []

        async def collect(evt: StoredEventEnvelope) -> None:
            seen.append(evt)

        runner = ProjectionRunner(FunctionProjection("test", collect), store, poll_interval_s=0.05)
        await runner.start()
        await asyncio.sleep(0.2)
        await runner.stop()
        assert len(seen) == 3
