# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`RedisStreamsEventBus` using mock redis client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyfly.eda.ports.outbound import EventPublisher


def _bus() -> object:
    # ``redis.asyncio`` is imported in the constructor; patch at the source.
    with patch("redis.asyncio.Redis.from_url", return_value=MagicMock()):
        from pyfly.eda.adapters.redis import RedisStreamsEventBus

        return RedisStreamsEventBus(url="redis://localhost:6379/0", streams=["pyfly.events"])


class TestRedisStreamsEventBus:
    def test_protocol_compliance(self) -> None:
        bus = _bus()
        assert isinstance(bus, EventPublisher)

    @pytest.mark.asyncio
    async def test_publish_writes_envelope_to_stream(self) -> None:
        bus = _bus()
        bus._client.xadd = AsyncMock()  # type: ignore[attr-defined]
        bus._started = True

        await bus.publish(
            destination="orders",
            event_type="order.created",
            payload={"id": 1},
            headers={"x-tenant": "acme"},
        )

        bus._client.xadd.assert_awaited_once()
        args, kwargs = bus._client.xadd.await_args
        assert args[0] == "orders"
        envelope_bytes = args[1][b"envelope"]
        envelope = json.loads(envelope_bytes)
        assert envelope["event_type"] == "order.created"
        assert envelope["destination"] == "orders"
        assert envelope["payload"] == {"id": 1}

    @pytest.mark.asyncio
    async def test_start_creates_consumer_group_busygroup_tolerant(self) -> None:
        bus = _bus()

        async def raise_busy(**_kwargs):
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")

        bus._client.xgroup_create = AsyncMock(side_effect=raise_busy)  # type: ignore[attr-defined]
        bus._client.aclose = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        # Replace the consume loop with a no-op so start() can return
        # without spinning the real xreadgroup loop.

        async def noop_loop() -> None:
            return None

        bus._consume_loop = noop_loop  # type: ignore[assignment]
        # Should not raise even though the group already exists.
        await bus.start()
        await bus.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_consume_task(self) -> None:
        bus = _bus()
        bus._client.xgroup_create = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        bus._client.aclose = AsyncMock(return_value=None)  # type: ignore[attr-defined]

        bus.subscribe("*", AsyncMock())

        async def long_loop() -> None:
            await asyncio.sleep(60)

        bus._consume_loop = long_loop  # type: ignore[assignment]
        await bus.start()
        await bus.stop()
        assert bus._consume_task is None
