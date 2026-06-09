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
"""Item 4 — EdaCommandEventPublisher round-trip test against a real InMemoryEventBus.

No Docker required.  Replaces fake-only coverage with an end-to-end
in-process EDA round trip.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cqrs.event.publisher import EdaCommandEventPublisher
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.types import EventEnvelope

# ---------------------------------------------------------------------------
# Sample domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderCreatedEvent:
    """Domain event emitted when an order is created."""

    order_id: str
    amount: float

    @property
    def event_type(self) -> str:
        return "order.created"


@dataclass(frozen=True)
class SimpleEvent:
    """Domain event without an explicit event_type attribute."""

    value: int


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEdaCommandEventPublisherLive:
    """EdaCommandEventPublisher integrated with a real InMemoryEventBus."""

    @pytest.fixture
    async def bus(self) -> InMemoryEventBus:
        b = InMemoryEventBus()
        await b.start()
        return b

    async def test_publish_sends_envelope_to_subscriber(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("order.created", handler)
        publisher = EdaCommandEventPublisher(bus)

        event = OrderCreatedEvent(order_id="ord-1", amount=99.99)
        await publisher.publish(event)

        assert len(received) == 1
        env = received[0]
        assert env.event_type == "order.created"
        assert env.payload == {"order_id": "ord-1", "amount": 99.99}
        assert env.destination == "cqrs.events"  # default destination

    async def test_publish_with_explicit_destination_override(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("order.created", handler)
        publisher = EdaCommandEventPublisher(bus)

        event = OrderCreatedEvent(order_id="ord-2", amount=50.0)
        await publisher.publish(event, destination="orders.events")

        assert len(received) == 1
        env = received[0]
        assert env.destination == "orders.events"

    async def test_publish_uses_class_name_when_no_event_type_attr(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("SimpleEvent", handler)
        publisher = EdaCommandEventPublisher(bus)

        event = SimpleEvent(value=42)
        await publisher.publish(event)

        assert len(received) == 1
        assert received[0].event_type == "SimpleEvent"
        assert received[0].payload == {"value": 42}

    async def test_wildcard_subscriber_receives_all_events(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def wildcard_handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("*", wildcard_handler)
        publisher = EdaCommandEventPublisher(bus)

        await publisher.publish(OrderCreatedEvent(order_id="ord-3", amount=10.0))
        await publisher.publish(SimpleEvent(value=1))

        assert len(received) == 2
        event_types = {e.event_type for e in received}
        assert "order.created" in event_types
        assert "SimpleEvent" in event_types

    async def test_non_matching_subscriber_not_called(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("other.event", handler)
        publisher = EdaCommandEventPublisher(bus)

        await publisher.publish(OrderCreatedEvent(order_id="ord-4", amount=5.0))

        assert len(received) == 0

    async def test_default_destination_can_be_customised(self, bus: InMemoryEventBus) -> None:
        received: list[EventEnvelope] = []

        async def handler(envelope: EventEnvelope) -> None:
            received.append(envelope)

        bus.subscribe("order.created", handler)
        publisher = EdaCommandEventPublisher(bus, default_destination="custom.events")

        await publisher.publish(OrderCreatedEvent(order_id="ord-5", amount=1.0))

        assert len(received) == 1
        assert received[0].destination == "custom.events"
