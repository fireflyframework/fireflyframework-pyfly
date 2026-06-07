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
"""Injectable ApplicationEventPublisher + arbitrary domain events (v26.06.41)."""

from __future__ import annotations

import pytest

from pyfly.container import service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import ApplicationEventPublisher, app_event_listener
from pyfly.core.config import Config


class OrderPlaced:
    """An arbitrary domain event — NOT an ApplicationEvent subclass."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id


_received: list[str] = []


@service
class OrderListener:
    @app_event_listener
    async def on_order(self, event: OrderPlaced) -> None:
        _received.append(event.order_id)


@service
class OrderService:
    def __init__(self, events: ApplicationEventPublisher) -> None:
        self.events = events

    async def place(self, order_id: str) -> None:
        await self.events.publish(OrderPlaced(order_id))


@pytest.mark.asyncio
async def test_injectable_publisher_delivers_domain_event() -> None:
    _received.clear()
    ctx = ApplicationContext(Config({}))
    ctx.register_bean(OrderListener)
    ctx.register_bean(OrderService)
    await ctx.start()

    svc = ctx.get_bean(OrderService)
    assert isinstance(svc.events, ApplicationEventPublisher)  # publisher was injected

    await svc.place("order-1")
    assert _received == ["order-1"]  # arbitrary domain event reached the @app_event_listener


@pytest.mark.asyncio
async def test_publisher_is_a_singleton_bean() -> None:
    ctx = ApplicationContext(Config({}))
    await ctx.start()
    assert isinstance(ctx.get_bean(ApplicationEventPublisher), ApplicationEventPublisher)
