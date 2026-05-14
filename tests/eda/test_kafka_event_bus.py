# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for :class:`KafkaEventBus` using mock aiokafka objects."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from pyfly.eda.adapters.kafka import KafkaEventBus
from pyfly.eda.ports.outbound import EventPublisher


class TestKafkaEventBus:
    def test_protocol_compliance(self) -> None:
        bus = KafkaEventBus(bootstrap_servers="localhost:9092")
        assert isinstance(bus, EventPublisher)

    @pytest.mark.asyncio
    async def test_publish_serialises_envelope(self) -> None:
        bus = KafkaEventBus(bootstrap_servers="localhost:9092", topics=["orders"])
        producer = AsyncMock()
        bus._producer = producer
        bus._started = True

        await bus.publish(
            destination="orders",
            event_type="order.created",
            payload={"id": 1},
            headers={"x-tenant": "acme"},
        )

        producer.send_and_wait.assert_awaited_once()
        call = producer.send_and_wait.await_args
        assert call.args[0] == "orders"
        envelope = json.loads(call.kwargs["value"])
        assert envelope["event_type"] == "order.created"
        assert envelope["payload"] == {"id": 1}
        assert envelope["destination"] == "orders"
        assert envelope["headers"] == {"x-tenant": "acme"}
        # Kafka record headers encode str values as bytes
        assert call.kwargs["headers"] == [("x-tenant", b"acme")]

    @pytest.mark.asyncio
    async def test_subscribe_appends_pattern_handler(self) -> None:
        bus = KafkaEventBus(bootstrap_servers="localhost:9092")

        async def handler(_envelope):
            return None

        bus.subscribe("order.*", handler)
        assert len(bus._handlers) == 1
        assert bus._handlers[0] == ("order.*", handler)

    @pytest.mark.asyncio
    async def test_publish_auto_starts(self) -> None:
        bus = KafkaEventBus(bootstrap_servers="localhost:9092")

        started = {"v": False}

        async def fake_start() -> None:
            started["v"] = True
            bus._producer = AsyncMock()
            bus._started = True

        bus.start = fake_start  # type: ignore[assignment]
        await bus.publish("t", "e", {})
        assert started["v"] is True
