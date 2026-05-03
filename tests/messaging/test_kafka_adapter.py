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
"""Tests for KafkaAdapter using mock aiokafka objects."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pyfly.messaging.adapters.kafka import KafkaAdapter
from pyfly.messaging.ports.outbound import MessageBrokerPort


class TestKafkaAdapter:
    def test_protocol_compliance(self) -> None:
        adapter = KafkaAdapter(bootstrap_servers="localhost:9092")
        assert isinstance(adapter, MessageBrokerPort)

    @pytest.mark.asyncio
    async def test_publish_sends_to_producer(self) -> None:
        adapter = KafkaAdapter(bootstrap_servers="localhost:9092")
        mock_producer = AsyncMock()
        adapter._producer = mock_producer
        await adapter.publish("orders", b'{"id": 1}', key=b"k1")
        mock_producer.send_and_wait.assert_called_once_with(
            "orders",
            value=b'{"id": 1}',
            key=b"k1",
            headers=None,
        )

    @pytest.mark.asyncio
    async def test_publish_with_headers(self) -> None:
        adapter = KafkaAdapter(bootstrap_servers="localhost:9092")
        mock_producer = AsyncMock()
        adapter._producer = mock_producer
        await adapter.publish("t", b"v", headers={"type": "test"})
        call_kwargs = mock_producer.send_and_wait.call_args
        assert call_kwargs.kwargs["headers"] == [("type", b"test")]

    @pytest.mark.asyncio
    async def test_subscribe_registers_handler(self) -> None:
        adapter = KafkaAdapter(bootstrap_servers="localhost:9092")

        async def handler(msg):
            pass

        await adapter.subscribe("orders", handler, group="g1")
        assert len(adapter._handlers) == 1
        assert adapter._handlers[0] == ("orders", handler, "g1")
