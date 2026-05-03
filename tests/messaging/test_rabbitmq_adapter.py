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
"""Tests for RabbitMQAdapter using mock aio-pika objects."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pyfly.messaging.adapters.rabbitmq import RabbitMQAdapter
from pyfly.messaging.ports.outbound import MessageBrokerPort


class TestRabbitMQAdapter:
    def test_protocol_compliance(self) -> None:
        adapter = RabbitMQAdapter(url="amqp://guest:guest@localhost/")
        assert isinstance(adapter, MessageBrokerPort)

    @pytest.mark.asyncio
    async def test_publish_sends_message(self) -> None:
        adapter = RabbitMQAdapter(url="amqp://localhost/")
        mock_exchange = AsyncMock()
        adapter._exchange = mock_exchange
        await adapter.publish("orders", b'{"id": 1}', key=b"order.created")
        mock_exchange.publish.assert_called_once()
        call_args = mock_exchange.publish.call_args
        assert call_args.kwargs["routing_key"] == "orders"

    @pytest.mark.asyncio
    async def test_subscribe_registers_handler(self) -> None:
        adapter = RabbitMQAdapter(url="amqp://localhost/")

        async def handler(msg):
            pass

        await adapter.subscribe("orders", handler, group="order-service")
        assert len(adapter._handlers) == 1

    @pytest.mark.asyncio
    async def test_publish_with_headers(self) -> None:
        adapter = RabbitMQAdapter(url="amqp://localhost/")
        mock_exchange = AsyncMock()
        adapter._exchange = mock_exchange
        await adapter.publish("t", b"data", headers={"type": "test"})
        mock_exchange.publish.assert_called_once()
