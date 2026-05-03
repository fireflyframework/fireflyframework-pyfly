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
"""Tests for EDA decorators."""

import pytest

from pyfly.eda import EventEnvelope
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.decorators import event_listener, event_publisher, publish_result


class TestEventPublisherDecorator:
    @pytest.mark.asyncio
    async def test_publishes_before_execution(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("user.create.command", capture)

        @event_publisher(bus=bus, destination="commands", event_type="user.create.command")
        async def create_user(data: dict) -> dict:
            return {"id": "1", **data}

        result = await create_user({"name": "Alice"})
        assert result == {"id": "1", "name": "Alice"}
        assert len(published) == 1
        assert published[0].payload == {"data": {"name": "Alice"}}


class TestPublishResultDecorator:
    @pytest.mark.asyncio
    async def test_publishes_return_value(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("user.created", capture)

        @publish_result(bus=bus, destination="user-events", event_type="user.created")
        async def create_user(name: str) -> dict:
            return {"id": "1", "name": name}

        result = await create_user("Alice")
        assert result == {"id": "1", "name": "Alice"}
        assert len(published) == 1
        assert published[0].payload == {"id": "1", "name": "Alice"}

    @pytest.mark.asyncio
    async def test_conditional_publish(self):
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []

        async def capture(event: EventEnvelope) -> None:
            published.append(event)

        bus.subscribe("user.created", capture)

        @publish_result(
            bus=bus,
            destination="events",
            event_type="user.created",
            condition=lambda result: result is not None,
        )
        async def maybe_create(should_create: bool) -> dict | None:
            return {"id": "1"} if should_create else None

        await maybe_create(True)
        assert len(published) == 1

        await maybe_create(False)
        assert len(published) == 1  # Not published


class TestEventListenerDecorator:
    @pytest.mark.asyncio
    async def test_registers_handler(self):
        bus = InMemoryEventBus()
        received: list[EventEnvelope] = []

        @event_listener(bus=bus, event_types=["user.created"])
        async def on_user_created(event: EventEnvelope) -> None:
            received.append(event)

        await bus.publish("events", "user.created", {"id": "1"})
        assert len(received) == 1
