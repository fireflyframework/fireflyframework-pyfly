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
"""Tests for InMemoryMessageBroker adapter."""

from __future__ import annotations

import pytest

from pyfly.messaging.adapters.memory import InMemoryMessageBroker
from pyfly.messaging.ports.outbound import MessageBrokerPort
from pyfly.messaging.types import Message


class TestInMemoryMessageBroker:
    def test_protocol_compliance(self) -> None:
        """InMemoryMessageBroker should satisfy the MessageBrokerPort protocol."""
        broker = InMemoryMessageBroker()
        assert isinstance(broker, MessageBrokerPort)

    async def test_publish_and_subscribe(self) -> None:
        """A subscribed handler should receive published messages."""
        broker = InMemoryMessageBroker()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await broker.subscribe("orders", handler)
        await broker.start()
        await broker.publish("orders", b"order-1")

        assert len(received) == 1
        assert received[0].topic == "orders"
        assert received[0].value == b"order-1"

    async def test_publish_with_key_and_headers(self) -> None:
        """Key and headers should be passed through to the Message."""
        broker = InMemoryMessageBroker()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await broker.subscribe("events", handler)
        await broker.start()
        await broker.publish(
            "events",
            b"payload",
            key=b"key-1",
            headers={"trace-id": "abc123"},
        )

        assert len(received) == 1
        assert received[0].key == b"key-1"
        assert received[0].headers == {"trace-id": "abc123"}

    async def test_multiple_subscribers(self) -> None:
        """All non-grouped subscribers should receive the same message."""
        broker = InMemoryMessageBroker()
        received_a: list[Message] = []
        received_b: list[Message] = []

        async def handler_a(msg: Message) -> None:
            received_a.append(msg)

        async def handler_b(msg: Message) -> None:
            received_b.append(msg)

        await broker.subscribe("topic", handler_a)
        await broker.subscribe("topic", handler_b)
        await broker.start()
        await broker.publish("topic", b"data")

        assert len(received_a) == 1
        assert len(received_b) == 1

    async def test_no_cross_topic_delivery(self) -> None:
        """A handler subscribed to one topic should not receive messages from another."""
        broker = InMemoryMessageBroker()
        received: list[Message] = []

        async def handler(msg: Message) -> None:
            received.append(msg)

        await broker.subscribe("topic-a", handler)
        await broker.start()
        await broker.publish("topic-b", b"data")

        assert len(received) == 0

    async def test_consumer_group_delivers_once(self) -> None:
        """Within a consumer group, only one handler should receive each message."""
        broker = InMemoryMessageBroker()
        received_a: list[Message] = []
        received_b: list[Message] = []

        async def handler_a(msg: Message) -> None:
            received_a.append(msg)

        async def handler_b(msg: Message) -> None:
            received_b.append(msg)

        await broker.subscribe("orders", handler_a, group="workers")
        await broker.subscribe("orders", handler_b, group="workers")
        await broker.start()
        await broker.publish("orders", b"msg-1")

        total = len(received_a) + len(received_b)
        assert total == 1, "Exactly one handler in the group should receive the message"

    async def test_publish_before_start_raises(self) -> None:
        """Publishing before start() should raise RuntimeError."""
        broker = InMemoryMessageBroker()
        with pytest.raises(RuntimeError, match="Broker is not running"):
            await broker.publish("topic", b"data")

    async def test_start_and_stop_lifecycle(self) -> None:
        """start() and stop() should toggle the running state."""
        broker = InMemoryMessageBroker()

        assert broker._running is False
        await broker.start()
        assert broker._running is True
        await broker.stop()
        assert broker._running is False
