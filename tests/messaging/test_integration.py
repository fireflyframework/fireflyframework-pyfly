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
"""End-to-end messaging integration tests with ApplicationContext."""

from __future__ import annotations

import pytest

from pyfly.container.stereotypes import component, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.messaging import Message, message_listener
from pyfly.messaging.adapters.memory import InMemoryMessageBroker


class TestMessagingIntegration:
    @pytest.mark.asyncio
    async def test_publish_and_consume_via_context(self) -> None:
        received: list[Message] = []
        broker = InMemoryMessageBroker()

        @service
        class OrderService:
            async def place_order(self, order_id: str) -> str:
                await broker.publish("orders", f'{{"id": "{order_id}"}}'.encode())
                return order_id

        @component
        class OrderListener:
            @message_listener(topic="orders")
            async def on_order(self, msg: Message) -> None:
                received.append(msg)

        listener = OrderListener()
        handler = listener.on_order
        await broker.subscribe("orders", handler)
        await broker.start()

        ctx = ApplicationContext(Config())
        ctx.register_bean(OrderService)
        await ctx.start()

        svc = ctx.get_bean(OrderService)
        result = await svc.place_order("ORD-001")

        assert result == "ORD-001"
        assert len(received) == 1
        assert b"ORD-001" in received[0].value

    @pytest.mark.asyncio
    async def test_consumer_group_round_robin(self) -> None:
        broker = InMemoryMessageBroker()
        results: list[str] = []

        async def handler_a(msg: Message) -> None:
            results.append("a")

        async def handler_b(msg: Message) -> None:
            results.append("b")

        await broker.subscribe("t", handler_a, group="g1")
        await broker.subscribe("t", handler_b, group="g1")
        await broker.start()

        for _ in range(4):
            await broker.publish("t", b"msg")

        assert results.count("a") == 2
        assert results.count("b") == 2
