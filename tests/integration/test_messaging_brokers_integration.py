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
"""Real-backend round-trip + subscribe-after-start integration tests for Kafka/RabbitMQ adapters.

For each broker two tests are included:
(a) publish→subscribe round-trip (subscribe BEFORE start).
(b) subscribe-after-start lifecycle (start FIRST, then subscribe, then publish).

Gated by ``@requires_docker``; collected only under ``-m integration``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pyfly.messaging.types import Message
from pyfly.testing import requires_docker

# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_kafka_adapter_publish_subscribe_round_trip(kafka_url: str) -> None:
    """KafkaAdapter: subscribe before start → publish → handler receives message."""
    from pyfly.messaging.adapters.kafka import KafkaAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = KafkaAdapter(bootstrap_servers=kafka_url)
    await adapter.subscribe(topic, handler, group=group)
    try:
        await adapter.start()
        await adapter.publish(topic, b'{"id": 1}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 1}'
    assert received[0].topic == topic


@requires_docker
@pytest.mark.asyncio
async def test_kafka_adapter_subscribe_after_start(kafka_url: str) -> None:
    """KafkaAdapter: start FIRST, subscribe after → publish → handler receives message."""
    from pyfly.messaging.adapters.kafka import KafkaAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it.{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = KafkaAdapter(bootstrap_servers=kafka_url)
    try:
        await adapter.start()
        # Subscribe after start — must spin up a consumer immediately.
        await adapter.subscribe(topic, handler, group=group)
        await adapter.publish(topic, b'{"id": 2}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 2}'
    assert received[0].topic == topic


# ---------------------------------------------------------------------------
# RabbitMQ
# ---------------------------------------------------------------------------


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_adapter_publish_subscribe_round_trip(amqp_url: str) -> None:
    """RabbitMQAdapter: subscribe before start → publish → handler receives message."""
    from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it-q-{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = RabbitMQAdapter(url=amqp_url)
    await adapter.subscribe(topic, handler, group=group)
    try:
        await adapter.start()
        await adapter.publish(topic, b'{"id": 1}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 1}'
    assert received[0].topic == topic


@requires_docker
@pytest.mark.asyncio
async def test_rabbitmq_adapter_subscribe_after_start(amqp_url: str) -> None:
    """RabbitMQAdapter: start FIRST, subscribe after → publish → handler receives message."""
    from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter

    topic = f"it.{uuid.uuid4().hex[:8]}"
    group = f"it-q-{uuid.uuid4().hex[:8]}"

    received: list[Message] = []
    done = asyncio.Event()

    async def handler(msg: Message) -> None:
        received.append(msg)
        done.set()

    adapter = RabbitMQAdapter(url=amqp_url)
    try:
        await adapter.start()
        # Subscribe after start — must bind its own queue immediately.
        await adapter.subscribe(topic, handler, group=group)
        await adapter.publish(topic, b'{"id": 2}')
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await adapter.stop()

    assert len(received) == 1
    assert received[0].value == b'{"id": 2}'
    assert received[0].topic == topic
