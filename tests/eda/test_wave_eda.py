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
"""Regression tests for EDA fixes (#134 discovery, #138 serializer, #140 timing)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.container.stereotypes import service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.decorators import event_listener, event_publisher
from pyfly.eda.ports.outbound import EventPublisher

_RECEIVED: list[str] = []


@service
class UserListener:
    @event_listener(["user.*"])
    async def on_user(self, envelope: Any) -> None:
        _RECEIVED.append(envelope.event_type)


@pytest.mark.asyncio
async def test_event_listener_auto_discovered_and_subscribed() -> None:
    _RECEIVED.clear()
    ctx = ApplicationContext(Config({"pyfly": {"eda": {"provider": "memory"}}}))
    ctx.register_bean(UserListener)
    await ctx.start()
    try:
        publisher = ctx.get_bean(EventPublisher)
        await publisher.publish("pyfly.events", "user.created", {"id": 1})
        assert _RECEIVED == ["user.created"]  # audit #134
    finally:
        await ctx.stop()


@pytest.mark.asyncio
async def test_event_publisher_after_includes_result() -> None:
    bus = InMemoryEventBus()
    seen: list[dict] = []

    async def capture(envelope: Any) -> None:
        seen.append(envelope.payload)

    bus.subscribe("order.placed", capture)

    @event_publisher(bus=bus, destination="orders", event_type="order.placed", timing="AFTER")
    async def place(amount: int) -> str:
        return f"order-{amount}"

    await place(42)
    # The AFTER payload carries the method result, not just the args (#140).
    assert seen[0]["result"] == "order-42"
    assert seen[0]["amount"] == 42


def test_serialization_format_selection() -> None:
    cfg = Config({"pyfly": {"eda": {"serialization-format": "avro"}}})
    serializer = EdaAutoConfiguration._make_serializer(cfg)
    assert type(serializer).__name__ == "AvroEventSerializer"  # audit #138

    default = EdaAutoConfiguration._make_serializer(Config({}))
    assert type(default).__name__ == "JsonEventSerializer"
