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
"""Tests for :class:`RabbitMqEventBus` using mock aio-pika objects."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus
from pyfly.eda.ports.outbound import EventPublisher


def _make_mocks() -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    """Return (connection, channel, exchange, queue) mocks."""
    mock_queue = AsyncMock()
    mock_exchange = AsyncMock()
    mock_channel = AsyncMock()
    mock_channel.declare_exchange = AsyncMock(return_value=mock_exchange)
    mock_channel.declare_queue = AsyncMock(return_value=mock_queue)
    mock_connection = AsyncMock()
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    return mock_connection, mock_channel, mock_exchange, mock_queue


class TestRabbitMqEventBus:
    def test_protocol_compliance(self) -> None:
        bus = RabbitMqEventBus()
        assert isinstance(bus, EventPublisher)

    def test_subscribe_registers_handler(self) -> None:
        bus = RabbitMqEventBus()

        async def handler(envelope):  # type: ignore[no-untyped-def]
            return None

        bus.subscribe("order.*", handler)
        assert len(bus._handlers) == 1
        assert bus._handlers[0] == ("order.*", handler)

    def test_subscribe_multiple_patterns(self) -> None:
        bus = RabbitMqEventBus()

        async def h1(envelope):  # type: ignore[no-untyped-def]
            pass

        async def h2(envelope):  # type: ignore[no-untyped-def]
            pass

        bus.subscribe("order.*", h1)
        bus.subscribe("payment.*", h2)
        assert len(bus._handlers) == 2

    @pytest.mark.asyncio
    async def test_publish_builds_envelope_and_calls_exchange(self) -> None:
        bus = RabbitMqEventBus(destinations=["orders"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()

        await bus.publish(
            destination="orders",
            event_type="order.created",
            payload={"id": 1},
            headers={"x-tenant": "acme"},
        )

        mock_exchange.publish.assert_awaited_once()
        call_args = mock_exchange.publish.call_args
        assert call_args.kwargs["routing_key"] == "orders"

        # Verify the serialized body contains the correct envelope fields
        published_message = call_args.args[0]
        body = json.loads(published_message.body.decode("utf-8"))
        assert body["event_type"] == "order.created"
        assert body["payload"] == {"id": 1}
        assert body["destination"] == "orders"
        assert body["headers"] == {"x-tenant": "acme"}

    @pytest.mark.asyncio
    async def test_publish_auto_starts(self) -> None:
        bus = RabbitMqEventBus()

        started: dict[str, bool] = {"v": False}

        async def fake_start() -> None:
            started["v"] = True
            bus._exchange = AsyncMock()
            bus._started = True

        bus.start = fake_start  # type: ignore[method-assign]
        await bus.publish("t", "e", {})
        assert started["v"] is True

    @pytest.mark.asyncio
    async def test_start_declares_exchange_and_queues(self) -> None:
        bus = RabbitMqEventBus(
            url="amqp://test/",
            exchange_name="test-exchange",
            destinations=["orders", "payments"],
            group="svc",
        )
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        with patch("aio_pika.connect_robust", return_value=mock_connection) as mock_connect:
            import aio_pika

            with patch("aio_pika.ExchangeType") as mock_et:
                mock_et.DIRECT = aio_pika.ExchangeType.DIRECT
                await bus.start()

        mock_connect.assert_awaited_once_with("amqp://test/")
        mock_connection.channel.assert_awaited_once()
        mock_channel.declare_exchange.assert_awaited_once()

        # Two queues declared — one per destination
        assert mock_channel.declare_queue.await_count == 2
        queue_names = [
            call.args[0] if call.args else call.kwargs.get("name")
            for call in mock_channel.declare_queue.await_args_list
        ]
        assert "svc.orders" in queue_names
        assert "svc.payments" in queue_names

        # Each queue was bound
        assert mock_queue.bind.await_count == 2
        # Each queue was consumed
        assert mock_queue.consume.await_count == 2

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        bus = RabbitMqEventBus(destinations=["orders"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()
            await bus.start()

        # connect_robust called only once
        assert mock_connection.channel.await_count == 1

    @pytest.mark.asyncio
    async def test_stop_closes_connection(self) -> None:
        bus = RabbitMqEventBus(destinations=["orders"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()

        await bus.stop()

        mock_connection.close.assert_awaited_once()
        assert bus._started is False
        assert bus._connection is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started_is_safe(self) -> None:
        bus = RabbitMqEventBus()
        # Should not raise
        await bus.stop()
        assert bus._started is False

    @pytest.mark.asyncio
    async def test_message_handler_dispatches_to_matching_subscribers(self) -> None:
        """Simulate incoming message dispatch via the on_message closure."""
        bus = RabbitMqEventBus(destinations=["orders"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        received: list[str] = []

        async def handler(envelope):  # type: ignore[no-untyped-def]
            received.append(envelope.event_type)

        bus.subscribe("order.*", handler)

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()

        # Extract the on_message callback registered via queue.consume
        consume_call = mock_queue.consume.await_args
        on_message = consume_call.args[0]

        # Build a fake incoming message
        from pyfly.eda.serializers import JsonEventSerializer
        from pyfly.eda.types import EventEnvelope

        serializer = JsonEventSerializer()
        envelope = EventEnvelope(
            event_type="order.created",
            payload={"id": 42},
            destination="orders",
        )
        raw_body = serializer.serialize(envelope)

        fake_message = MagicMock()
        fake_message.body = raw_body
        fake_message.process = MagicMock(return_value=AsyncMock().__aenter__.return_value)
        # Make process() work as an async context manager
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        fake_message.process = MagicMock(return_value=cm)

        await on_message(fake_message)
        assert received == ["order.created"]

    @pytest.mark.asyncio
    async def test_message_handler_does_not_dispatch_non_matching(self) -> None:
        """Handler registered for 'payment.*' should not receive 'order.created'."""
        bus = RabbitMqEventBus(destinations=["events"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        received: list[str] = []

        async def handler(envelope):  # type: ignore[no-untyped-def]
            received.append(envelope.event_type)

        bus.subscribe("payment.*", handler)

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()

        consume_call = mock_queue.consume.await_args
        on_message = consume_call.args[0]

        from pyfly.eda.serializers import JsonEventSerializer
        from pyfly.eda.types import EventEnvelope

        serializer = JsonEventSerializer()
        envelope = EventEnvelope(
            event_type="order.created",
            payload={"id": 1},
            destination="events",
        )
        raw_body = serializer.serialize(envelope)

        fake_message = MagicMock()
        fake_message.body = raw_body
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        fake_message.process = MagicMock(return_value=cm)

        await on_message(fake_message)
        assert received == []

    @pytest.mark.asyncio
    async def test_deserialization_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        """A corrupt message body should log an error and not propagate."""
        import logging

        bus = RabbitMqEventBus(destinations=["orders"])
        mock_connection, mock_channel, mock_exchange, mock_queue = _make_mocks()

        with patch("aio_pika.connect_robust", return_value=mock_connection):
            await bus.start()

        consume_call = mock_queue.consume.await_args
        on_message = consume_call.args[0]

        fake_message = MagicMock()
        fake_message.body = b"not-valid-json"
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        fake_message.process = MagicMock(return_value=cm)

        with caplog.at_level(logging.ERROR, logger="pyfly.eda.adapters.rabbitmq"):
            await on_message(fake_message)

        assert any("Failed to deserialize" in r.message for r in caplog.records)
