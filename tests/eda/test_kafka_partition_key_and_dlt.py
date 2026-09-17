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
"""Kafka parity with the rest of the Firefly family: keyed records and a dead-letter topic.

Three things the Kafka adapter did not do until this suite existed, each found by a Python
service sharing topics with a LaraFly (PHP) control plane:

* ``publish`` sent no partition key, so records were round-robined across partitions and the
  per-aggregate ordering every other Firefly publisher guarantees was lost on the Python side.
* the consume loop ``continue``d past a record it could not deserialise — with auto-commit on,
  the offset advanced and the message was gone before any listener ran.
* the stock serializer read ``raw["event_id"]`` and died with ``KeyError`` on a LaraFly envelope
  (``eventId``/``eventType``, camelCase).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pyfly.core.config import Config
from pyfly.eda.adapters.kafka import KafkaEventBus, partition_key
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.serializers import EnvelopeDecodeError, FireflyJsonEventSerializer, JsonEventSerializer
from pyfly.eda.types import EventEnvelope


@dataclass
class FakeRecord:
    topic: str
    offset: int
    value: bytes
    key: bytes | None = None
    headers: list[tuple[str, bytes]] = field(default_factory=list)


class FakeConsumer:
    """An async iterator over canned records, like ``AIOKafkaConsumer``."""

    def __init__(self, records: list[FakeRecord]) -> None:
        self._records = records

    def __aiter__(self) -> FakeConsumer:
        return self

    async def __anext__(self) -> FakeRecord:
        if not self._records:
            raise StopAsyncIteration
        return self._records.pop(0)


def _bus_with_fakes(records: list[FakeRecord], **kwargs: Any) -> tuple[KafkaEventBus, AsyncMock]:
    bus = KafkaEventBus(bootstrap_servers="localhost:9092", topics=["orders"], **kwargs)
    producer = AsyncMock()
    bus._producer = producer
    bus._consumer = FakeConsumer(records)
    bus._started = True
    return bus, producer


class TestPartitionKey:
    def test_key_precedence_matches_larafly(self) -> None:
        """``partition_key`` header, then ``x-correlation-id``, then the event type — LaraFly's rule."""
        assert partition_key("order.created", {"partition_key": "room-1", "x-correlation-id": "c"}) == "room-1"
        assert partition_key("order.created", {"x-correlation-id": "corr-9"}) == "corr-9"
        assert partition_key("order.created", {}) == "order.created"
        assert partition_key("order.created", None) == "order.created"
        # PHP's ?? keeps an empty string; so do we.
        assert partition_key("order.created", {"partition_key": ""}) == ""

    async def test_publish_sends_the_partition_key(self) -> None:
        bus, producer = _bus_with_fakes([])
        await bus.publish("orders", "order.created", {"id": 1}, headers={"partition_key": "customer-42"})
        call = producer.send_and_wait.await_args
        assert call.kwargs["key"] == b"customer-42"

    async def test_publish_falls_back_to_the_event_type(self) -> None:
        bus, producer = _bus_with_fakes([])
        await bus.publish("orders", "order.created", {"id": 1})
        assert producer.send_and_wait.await_args.kwargs["key"] == b"order.created"

    async def test_an_explicit_key_wins_over_the_headers(self) -> None:
        bus, producer = _bus_with_fakes([])
        await bus.publish("orders", "order.created", {"id": 1}, headers={"partition_key": "h"}, key="explicit")
        assert producer.send_and_wait.await_args.kwargs["key"] == b"explicit"

    async def test_the_key_header_name_is_configurable(self) -> None:
        bus, producer = _bus_with_fakes([], partition_key_header="x-room-id")
        await bus.publish("orders", "order.created", {"id": 1}, headers={"x-room-id": "room-7", "partition_key": "no"})
        assert producer.send_and_wait.await_args.kwargs["key"] == b"room-7"


class TestDeadLetterTopic:
    async def test_a_poison_record_is_dead_lettered_verbatim_before_the_loop_moves_on(self, caplog: Any) -> None:
        poison = FakeRecord("orders", 41, b"\xff\xfe not json", key=b"k", headers=[("x-tenant", b"acme")])
        good = EventEnvelope(event_type="order.created", payload={"id": 1}, destination="orders")
        fine = FakeRecord("orders", 42, JsonEventSerializer().serialize(good))
        bus, producer = _bus_with_fakes([poison, fine])
        seen: list[str] = []

        async def handler(envelope: EventEnvelope) -> None:
            seen.append(envelope.event_id)

        bus.subscribe("order.*", handler)
        with caplog.at_level(logging.WARNING):
            await bus._consume_loop()

        # The poison record went to <topic>.DLT with its bytes, key and headers untouched, plus
        # the reason and the origin so an operator can replay it.
        producer.send_and_wait.assert_awaited_once()
        call = producer.send_and_wait.await_args
        assert call.args[0] == "orders.DLT"
        assert call.kwargs["value"] == b"\xff\xfe not json"
        assert call.kwargs["key"] == b"k"
        headers = dict(call.kwargs["headers"])
        assert headers["x-tenant"] == b"acme"
        assert headers["x-dlt-reason"] == b"EnvelopeDecodeError"
        assert headers["x-dlt-source-topic"] == b"orders"
        assert headers["x-dlt-source-offset"] == b"41"
        assert bus.dlt_published == 1
        assert bus.dlt_publish_failures == 0
        # The good record after it was still delivered.
        assert seen == [good.event_id]
        assert any("dead_lettered" in r.getMessage() for r in caplog.records)

    async def test_the_dlt_publish_is_retried_and_the_failure_is_counted(self, caplog: Any, monkeypatch: Any) -> None:
        poison = FakeRecord("orders", 7, b"{")
        bus, producer = _bus_with_fakes([poison])
        producer.send_and_wait.side_effect = ConnectionError("broker away")
        sleeps: list[float] = []

        async def no_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        with caplog.at_level(logging.CRITICAL):
            await bus._consume_loop()

        assert producer.send_and_wait.await_count == 3
        assert sleeps == [0.5, 1.0]
        assert bus.dlt_published == 0
        assert bus.dlt_publish_failures == 1
        assert any("dead_letter_publish_failed" in r.getMessage() for r in caplog.records)

    async def test_the_dlt_can_be_switched_off(self, caplog: Any) -> None:
        poison = FakeRecord("orders", 7, b"{")
        bus, producer = _bus_with_fakes([poison], dlt_suffix=None)
        with caplog.at_level(logging.ERROR):
            await bus._consume_loop()
        producer.send_and_wait.assert_not_awaited()
        assert any("Failed to deserialize" in r.getMessage() for r in caplog.records)

    async def test_a_handler_failure_is_not_dead_lettered(self) -> None:
        """Dead-lettering is for bytes nobody can read; a handler that raises is its own concern."""
        good = EventEnvelope(event_type="order.created", payload={"id": 1}, destination="orders")
        bus, producer = _bus_with_fakes([FakeRecord("orders", 1, JsonEventSerializer().serialize(good))])

        async def boom(_: EventEnvelope) -> None:
            raise RuntimeError("handler broke")

        bus.subscribe("order.*", boom)
        await bus._consume_loop()
        producer.send_and_wait.assert_not_awaited()

    def test_auto_configuration_reads_the_dlt_and_key_settings(self) -> None:
        config = Config(
            {
                "pyfly": {
                    "eda": {
                        "provider": "kafka",
                        "kafka": {
                            "bootstrap-servers": "broker:9092",
                            "partition-key-header": "x-room-id",
                            "dlt": {"enabled": "false"},
                        },
                    }
                }
            }
        )
        bus = EdaAutoConfiguration().event_publisher(config)
        assert isinstance(bus, KafkaEventBus)
        assert bus._partition_key_header == "x-room-id"
        assert bus._dlt_suffix is None

        config = Config({"pyfly": {"eda": {"provider": "kafka", "kafka": {"dlt": {"suffix": ".dead"}}}}})
        bus = EdaAutoConfiguration().event_publisher(config)
        assert isinstance(bus, KafkaEventBus)
        assert bus._dlt_suffix == ".dead"
        assert bus._partition_key_header == "partition_key"


LARAFLY_ENVELOPE = (
    b'{"eventType":"dworkers.rooms.turn.contributed.v1","destination":"dworkers.room-events.v1",'
    b'"payload":{"roomId":"r1"},"headers":{"x-correlation-id":"c1","partition_key":"r1"},'
    b'"eventId":"019948a0-1b2c-7def-8abc-0123456789ab","timestamp":"2026-09-05T10:00:00+00:00"}'
)


class TestEnvelopeTolerantSerializer:
    def test_reads_a_larafly_envelope(self) -> None:
        envelope = JsonEventSerializer().deserialize(LARAFLY_ENVELOPE)
        assert envelope.event_type == "dworkers.rooms.turn.contributed.v1"
        assert envelope.event_id == "019948a0-1b2c-7def-8abc-0123456789ab"
        assert envelope.destination == "dworkers.room-events.v1"
        assert envelope.payload == {"roomId": "r1"}
        assert envelope.headers == {"x-correlation-id": "c1", "partition_key": "r1"}
        assert envelope.timestamp.isoformat() == "2026-09-05T10:00:00+00:00"

    def test_still_reads_its_own_envelope(self) -> None:
        original = EventEnvelope(event_type="a.b", payload={"x": 1}, destination="d", headers={"h": "v"})
        restored = JsonEventSerializer().deserialize(JsonEventSerializer().serialize(original))
        assert restored == original

    def test_php_empty_array_headers_and_payload_are_objects(self) -> None:
        raw = (
            b'{"eventType":"a.b","destination":"d","payload":[],"headers":[],'
            b'"eventId":"e","timestamp":"2026-01-01T00:00:00+00:00"}'
        )
        envelope = JsonEventSerializer().deserialize(raw)
        assert envelope.headers == {}
        assert envelope.payload == {}

    def test_a_missing_event_id_or_timestamp_is_filled_in(self) -> None:
        envelope = JsonEventSerializer().deserialize(b'{"event_type":"a.b","destination":"d","payload":{}}')
        assert len(envelope.event_id) == 36
        assert envelope.timestamp.tzinfo is not None

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            (b"\xff", "not valid UTF-8 JSON"),
            (b"[1, 2]", "must be a JSON object"),
            (b'{"payload":{},"destination":"d"}', "event type"),
            (b'{"event_type":"a","destination":"d","payload":[1]}', "'payload' must be a JSON object"),
            (b'{"event_type":"a","destination":"d","payload":{},"timestamp":"yesterday"}', "not ISO 8601"),
        ],
    )
    def test_a_malformed_envelope_raises_one_typed_error(self, raw: bytes, fragment: str) -> None:
        with pytest.raises(EnvelopeDecodeError, match=fragment):
            JsonEventSerializer().deserialize(raw)


class TestFireflyJsonEventSerializer:
    def test_writes_the_larafly_shape_in_larafly_order(self) -> None:
        envelope = EventEnvelope(
            event_type="a.b",
            payload={"x": 1},
            destination="d",
            event_id="e-1",
            headers={"h": "v"},
            timestamp=JsonEventSerializer().deserialize(LARAFLY_ENVELOPE).timestamp,
        )
        raw = FireflyJsonEventSerializer().serialize(envelope)
        expected = (
            b'{"eventType":"a.b","destination":"d","payload":{"x":1},"headers":{"h":"v"},'
            b'"eventId":"e-1","timestamp":"2026-09-05T10:00:00+00:00"}'
        )
        assert raw == expected
        assert list(json.loads(raw)) == ["eventType", "destination", "payload", "headers", "eventId", "timestamp"]

    def test_round_trips_through_the_tolerant_reader(self) -> None:
        original = EventEnvelope(event_type="a.b", payload={"x": 1}, destination="d", headers={"h": "v"})
        restored = FireflyJsonEventSerializer().deserialize(FireflyJsonEventSerializer().serialize(original))
        # DATE_ATOM has no sub-second precision, so the timestamp is truncated on purpose.
        assert restored.timestamp == original.timestamp.replace(microsecond=0)
        assert (restored.event_type, restored.payload, restored.destination, restored.event_id, restored.headers) == (
            original.event_type,
            original.payload,
            original.destination,
            original.event_id,
            original.headers,
        )

    def test_selectable_through_configuration(self) -> None:
        config = Config({"pyfly": {"eda": {"provider": "memory", "serialization-format": "firefly-json"}}})
        assert isinstance(EdaAutoConfiguration._make_serializer(config), FireflyJsonEventSerializer)
