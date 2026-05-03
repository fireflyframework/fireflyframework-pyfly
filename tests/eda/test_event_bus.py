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
"""Tests for event bus abstraction."""

import pytest

from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.types import EventEnvelope


class TestInMemoryEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self):
        bus = InMemoryEventBus()
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        bus.subscribe("user.created", handler)
        await bus.publish("user-events", "user.created", {"id": "123", "name": "Alice"})

        assert len(received) == 1
        assert received[0].event_type == "user.created"
        assert received[0].payload["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self):
        bus = InMemoryEventBus()
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        bus.subscribe("user.*", handler)
        await bus.publish("events", "user.created", {"id": "1"})
        await bus.publish("events", "user.updated", {"id": "1"})
        await bus.publish("events", "order.created", {"id": "2"})

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_no_subscribers(self):
        bus = InMemoryEventBus()
        # Should not raise
        await bus.publish("topic", "event.type", {"data": "value"})

    @pytest.mark.asyncio
    async def test_envelope_has_metadata(self):
        bus = InMemoryEventBus()
        received: list[EventEnvelope] = []

        async def handler(event: EventEnvelope) -> None:
            received.append(event)

        bus.subscribe("test.event", handler)
        await bus.publish("my-topic", "test.event", {"key": "val"})

        envelope = received[0]
        assert envelope.destination == "my-topic"
        assert envelope.event_id is not None
        assert envelope.timestamp is not None
