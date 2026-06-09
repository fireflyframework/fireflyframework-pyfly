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
"""Real-backend round-trip integration tests for all 4 EventPublisher buses.

Each test subscribes a handler, starts the bus, publishes an event, and asserts
the handler receives the correct envelope — exercising the full produce→consume
round-trip against real Docker-backed brokers.

Gated by ``@requires_docker``; collected only under ``-m integration``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pyfly.eda.types import EventEnvelope
from pyfly.testing import requires_docker


@requires_docker
@pytest.mark.asyncio
async def test_postgres_event_bus_round_trip(pg_url: str) -> None:
    """PostgresEventBus: publish→LISTEN/NOTIFY→handler round-trip."""
    from pyfly.eda.adapters.postgres import PostgresEventBus

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = PostgresEventBus(
        dsn=pg_url,
        destinations=["pyfly.events"],
        group="it",
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@requires_docker
@pytest.mark.asyncio
async def test_redis_event_bus_round_trip(redis_url: str) -> None:
    """RedisStreamsEventBus: publish→XREADGROUP→handler round-trip."""
    from pyfly.eda.adapters.redis import RedisStreamsEventBus

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = RedisStreamsEventBus(
        url=redis_url,
        streams=["pyfly.events"],
        group="it",
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        # Group is created at "$" so publish AFTER start to avoid missing the entry.
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@requires_docker
@pytest.mark.asyncio
async def test_kafka_event_bus_round_trip(kafka_url: str) -> None:
    """KafkaEventBus: publish→consume→handler round-trip on a unique topic."""
    from pyfly.eda.adapters.kafka import KafkaEventBus

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = KafkaEventBus(
        bootstrap_servers=kafka_url,
        topics=[topic],
        group=group,
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish(topic, "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_event_bus_round_trip(amqp_url: str) -> None:
    """RabbitMqEventBus: publish→queue→handler round-trip on a unique destination."""
    from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus

    destination = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    bus = RabbitMqEventBus(
        url=amqp_url,
        destinations=[destination],
        group=group,
    )
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish(destination, "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert received[0].payload == {"id": 1}
