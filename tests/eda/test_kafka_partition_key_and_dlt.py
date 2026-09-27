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
"""Kafka parity with the rest of the Firefly family: keyed records, a dead-letter topic, and no lost events.

Things the Kafka bus did not do until this suite existed:

* ``publish`` sent no partition key, so records were round-robined across partitions and the
  per-aggregate ordering every other Firefly publisher guarantees was lost on the Python side.
* the consume loop ``continue``d past a record it could not deserialise — with auto-commit on,
  the offset advanced and the message was gone before any listener ran.
* the stock serializer read ``raw["event_id"]`` and died with ``KeyError`` on a LaraFly envelope
  (``eventId``/``eventType``, camelCase).
* C008 (26.09.08): an ``@event_listener`` whose handler failed was logged and skipped while auto-commit
  committed past it, and a stop cancelled the handler in flight and then committed its offset. The bus
  now consumes through the listener container: the offset is committed after the handlers' unit of work
  commits, a failure is attempted again after a back-off and dead-lettered after the last attempt, and
  a stop drains the record in flight. The fake cluster below keeps positions and committed offsets, so
  these tests see what the broker would.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from pyfly.core.config import Config
from pyfly.data.transaction import current_unit_of_work
from pyfly.data.transactional import transactional
from pyfly.eda.adapters.kafka import KafkaEventBus, partition_key
from pyfly.eda.auto_configuration import EdaAutoConfiguration
from pyfly.eda.dlq import InMemoryEdaDeadLetterStore
from pyfly.eda.serializers import EnvelopeDecodeError, FireflyJsonEventSerializer, JsonEventSerializer
from pyfly.eda.types import EventEnvelope
from pyfly.messaging import listener_container
from pyfly.messaging.adapters import kafka as messaging_kafka
from pyfly.messaging.listener_container import FixedBackOff, ListenerContainerSettings, RetryPolicy
from tests.messaging.brokers import FakeKafkaCluster
from tests.messaging.listener_app import (
    Behavior,
    DeliveredRepository,
    boot,
    committed_bodies,
    event_bus_bean,
    event_listener_bean,
    eventually,
)
from tests.support.backend_matrix import SQLITE_FILE, RelationalBackend

TOPIC = "orders"
GROUP = "order-service"

FAST = ListenerContainerSettings(
    retry=RetryPolicy(max_attempts=3, backoff=FixedBackOff(0.01)), transactional=False, poll_timeout=0.05
)


def _bus(cluster: FakeKafkaCluster, **kwargs: Any) -> KafkaEventBus:
    kwargs.setdefault("settings", FAST)
    return KafkaEventBus(
        bootstrap_servers="fake:9092",
        topics=[TOPIC],
        group=GROUP,
        consumer_factory=cluster.consumer,
        producer_factory=cluster.producer,
        **kwargs,
    )


def _envelope(event_type: str = "order.created", **payload: Any) -> bytes:
    return JsonEventSerializer().serialize(EventEnvelope(event_type=event_type, payload=payload, destination=TOPIC))


class TestPartitionKey:
    def test_key_precedence_matches_larafly(self) -> None:
        """``partition_key`` header, then ``x-correlation-id``, then the event type — LaraFly's rule."""
        assert partition_key("order.created", {"partition_key": "room-1", "x-correlation-id": "c"}) == "room-1"
        assert partition_key("order.created", {"x-correlation-id": "corr-9"}) == "corr-9"
        assert partition_key("order.created", {}) == "order.created"
        assert partition_key("order.created", None) == "order.created"
        # PHP's ?? keeps an empty string; so do we.
        assert partition_key("order.created", {"partition_key": ""}) == ""

    async def _published_key(self, **kwargs: Any) -> bytes | None:
        cluster = FakeKafkaCluster()
        header = kwargs.pop("partition_key_header", None)
        bus = _bus(cluster, **({"partition_key_header": header} if header else {}))
        bus._producer = cluster.producer()
        await bus._producer.start()
        bus._started = True
        await bus.publish("orders", "order.created", {"id": 1}, **kwargs)
        [record] = cluster.records("orders")
        return record.key

    async def test_publish_sends_the_partition_key(self) -> None:
        assert await self._published_key(headers={"partition_key": "customer-42"}) == b"customer-42"

    async def test_publish_falls_back_to_the_event_type(self) -> None:
        assert await self._published_key() == b"order.created"

    async def test_an_explicit_key_wins_over_the_headers(self) -> None:
        assert await self._published_key(headers={"partition_key": "h"}, key="explicit") == b"explicit"

    async def test_the_key_header_name_is_configurable(self) -> None:
        key = await self._published_key(
            partition_key_header="x-room-id", headers={"x-room-id": "room-7", "partition_key": "no"}
        )
        assert key == b"room-7"


class TestDeadLetterTopic:
    async def test_a_poison_record_is_dead_lettered_verbatim_before_the_offset_moves_on(self, caplog: Any) -> None:
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, b"\xff\xfe not json", key=b"k", headers=[("x-tenant", b"acme")])
        good = EventEnvelope(event_type="order.created", payload={"id": 1}, destination=TOPIC)
        cluster.append(TOPIC, JsonEventSerializer().serialize(good))
        seen: list[str] = []

        async def handler(envelope: EventEnvelope) -> None:
            seen.append(envelope.event_id)

        bus = _bus(cluster)
        bus.subscribe("order.*", handler)
        with caplog.at_level(logging.WARNING):
            await bus.start()
            try:
                await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 2, what="both committed")
            finally:
                await bus.stop()

        # The poison record went to <topic>.DLT at once with its bytes, key and headers untouched, plus
        # the reason and the origin so an operator can replay it.
        [dead] = cluster.records(f"{TOPIC}.DLT")
        assert (dead.value, dead.key) == (b"\xff\xfe not json", b"k")
        headers = dict(dead.headers)
        assert headers["x-tenant"] == b"acme"
        assert headers["x-dlt-reason"] == b"EnvelopeDecodeError"
        assert headers["x-dlt-source-topic"] == b"orders"
        assert headers["x-dlt-source-offset"] == b"0"
        assert headers["x-dlt-attempts"] == b"1"
        assert (bus.dlt_published, bus.dlt_publish_failures) == (1, 0)
        # The good record after it was still delivered.
        assert seen == [good.event_id]
        assert any("dead_lettered" in r.getMessage() for r in caplog.records)

    async def test_a_failed_dlt_publish_is_retried_and_the_record_stays_uncommitted_until_it_lands(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(messaging_kafka, "DLT_BACKOFF_SECONDS", 0.0)
        monkeypatch.setattr(listener_container, "DEAD_LETTER_RETRY_DELAY", 0.05)
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, b"{")
        bus = _bus(cluster)
        await bus.start()
        try:
            bus._producer.failures = 3  # one round of three publish attempts
            await eventually(lambda: bus.dlt_publish_failures == 1, what="a failed round")
            assert cluster.committed_offset(GROUP, TOPIC) is None  # not lost: still uncommitted
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="dead-lettered, then committed")
        finally:
            await bus.stop()
        assert bus._producer is None
        assert [record.value for record in cluster.records(f"{TOPIC}.DLT")] == [b"{"]
        assert bus.dlt_published == 1

    async def test_the_dlt_can_be_switched_off(self, caplog: Any) -> None:
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, b"{")
        bus = _bus(cluster, dlt_suffix=None)
        with caplog.at_level(logging.ERROR):
            await bus.start()
            try:
                await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="skipped and committed")
            finally:
                await bus.stop()
        assert cluster.records(f"{TOPIC}.DLT") == []
        assert any("listener_delivery_skipped" in r.getMessage() for r in caplog.records)

    async def test_a_handler_that_keeps_failing_is_attempted_again_then_dead_lettered(self) -> None:
        """C008: the event is not skipped after one failure: three attempts, then ``<topic>.DLT``."""
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, _envelope(id=1))
        attempts: list[int] = []

        async def boom(_: EventEnvelope) -> None:
            attempts.append(1)
            raise RuntimeError("handler broke")

        bus = _bus(cluster)
        bus.subscribe("order.*", boom)
        await bus.start()
        try:
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="dead-lettered and committed")
        finally:
            await bus.stop()
        assert len(attempts) == 3
        [dead] = cluster.records(f"{TOPIC}.DLT")
        headers = dict(dead.headers)
        assert headers["x-dlt-reason"] == b"RuntimeError"
        assert headers["x-dlt-attempts"] == b"3"

    async def test_the_dead_letter_store_records_the_failed_event(self) -> None:
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, _envelope(id=7))
        store = InMemoryEdaDeadLetterStore()

        async def boom(_: EventEnvelope) -> None:
            raise RuntimeError("projection is broken")

        bus = _bus(cluster, dead_letter_store=store)
        bus.subscribe("order.*", boom)
        await bus.start()
        try:
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="dead-lettered and committed")
        finally:
            await bus.stop()
        [entry] = await store.list()
        assert entry.event.payload == {"id": 7}
        assert (entry.error_type, entry.error_message, entry.attempts) == ("RuntimeError", "projection is broken", 3)

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
                        "listener": {"shutdown-timeout": "4", "retry": {"max-attempts": "6"}},
                    }
                }
            }
        )
        store = InMemoryEdaDeadLetterStore()
        bus = EdaAutoConfiguration().event_publisher(config, store)
        assert isinstance(bus, KafkaEventBus)
        assert bus._partition_key_header == "x-room-id"
        assert bus._dlt_suffix is None
        assert bus._dead_letter_store is store
        assert (bus._settings.shutdown_timeout, bus._settings.retry.max_attempts) == (4.0, 6)

        config = Config({"pyfly": {"eda": {"provider": "kafka", "kafka": {"dlt": {"suffix": ".dead"}}}}})
        bus = EdaAutoConfiguration().event_publisher(config)
        assert isinstance(bus, KafkaEventBus)
        assert bus._dlt_suffix == ".dead"
        assert bus._partition_key_header == "partition_key"
        assert bus._dead_letter_store is None


@pytest.mark.backends(SQLITE_FILE)
class TestTransactionalEventListener:
    """C008 on a real application: ``@event_listener`` + ``@transactional`` + a repository on SQLite."""

    async def test_a_handler_that_fails_once_is_delivered_again_and_committed_once(
        self, relational_backend: RelationalBackend
    ) -> None:
        cluster = FakeKafkaCluster()
        for body in ("e1", "e2", "e3"):
            cluster.append(TOPIC, _envelope(body=body))
        behavior = Behavior(fail_first={"e2"})
        bus = _bus(cluster, settings=ListenerContainerSettings(retry=FAST.retry, poll_timeout=0.05))
        ctx = await boot(relational_backend, event_bus_bean(bus), event_listener_bean(behavior))
        try:
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 3, what="offset 3 committed")
        finally:
            await ctx.stop()
        assert await committed_bodies(relational_backend) == ["e1", "e2", "e3"]
        assert behavior.seen == ["e1", "e2", "e2", "e3"]
        assert cluster.consumers[0].enable_auto_commit is False

    async def test_a_stop_mid_handler_does_not_commit_the_offset_and_the_event_comes_back(
        self, relational_backend: RelationalBackend
    ) -> None:
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, _envelope(body="stuck"))
        settings = ListenerContainerSettings(retry=FAST.retry, poll_timeout=0.05, shutdown_timeout=0.2)
        stuck = Behavior(gates={"stuck": asyncio.Event()})
        ctx = await boot(
            relational_backend, event_bus_bean(_bus(cluster, settings=settings)), event_listener_bean(stuck)
        )
        await stuck.started_event("stuck").wait()
        await ctx.stop()
        assert cluster.committed_offset(GROUP, TOPIC) is None
        assert await committed_bodies(relational_backend) == []

        again = Behavior()
        ctx = await boot(
            relational_backend, event_bus_bean(_bus(cluster, settings=settings)), event_listener_bean(again)
        )
        try:
            await eventually(lambda: cluster.committed_offset(GROUP, TOPIC) == 1, what="delivered again")
        finally:
            await ctx.stop()
        assert await committed_bodies(relational_backend) == ["stuck"]

    async def test_deliveries_do_not_inherit_the_transaction_that_started_the_consumer(
        self, relational_backend: RelationalBackend
    ) -> None:
        """A bus that starts on its first publish, inside a request's ``@transactional``: its consumer runs
        in a detached task, so the event's handler gets a unit of its own, not the request's."""
        cluster = FakeKafkaCluster()
        behavior = Behavior()
        bus = _bus(cluster, settings=ListenerContainerSettings(retry=FAST.retry, poll_timeout=0.05))
        ctx = await boot(relational_backend, event_listener_bean(behavior))
        try:
            bus.subscribe("order.*", lambda envelope: behavior.handle(ctx.get_bean(DeliveredRepository), "late"))
            request_units: list[int] = []

            @transactional
            async def place_order() -> None:
                unit = current_unit_of_work()
                assert unit is not None
                request_units.append(unit.id)
                await bus.publish(TOPIC, "order.created", {"body": "late"})  # starts the bus lazily

            await place_order()
            await eventually(lambda: behavior.finished == ["late"], what="the event handled")
        finally:
            await bus.stop()
            await ctx.stop()
        [handler_unit] = behavior.units
        assert handler_unit is not None and handler_unit != request_units[0]
        assert await committed_bodies(relational_backend) == ["late"]


class TestStopAndPublish:
    async def test_a_publish_while_stopping_neither_restarts_the_consumer_nor_fails(self) -> None:
        cluster = FakeKafkaCluster()
        cluster.append(TOPIC, _envelope(id=1))
        gate = asyncio.Event()
        published: list[bool] = []

        async def handler(_: EventEnvelope) -> None:
            await gate.wait()
            await bus.publish("replies", "order.replied", {"id": 1})  # a handler in flight publishes
            published.append(True)

        bus = _bus(cluster)
        bus.subscribe("order.*", handler)
        await bus.start()
        await eventually(lambda: bool(cluster.consumers) and cluster.consumers[0].positions, what="consuming")
        stopping = asyncio.create_task(bus.stop())
        await asyncio.sleep(0.05)
        gate.set()
        await stopping
        assert published == [True]
        assert [record.topic for record in cluster.records("replies")] == ["replies"]
        assert len(cluster.consumers) == 1  # no consumer was started by the publish
        assert cluster.committed_offset(GROUP, TOPIC) == 1

    async def test_a_publish_after_stop_opens_the_producer_only(self) -> None:
        cluster = FakeKafkaCluster()
        bus = _bus(cluster)
        await bus.start()
        await bus.stop()
        await bus.publish("goodbye", "app.stopping", {})  # a @pre_destroy announcing the shutdown
        assert [record.topic for record in cluster.records("goodbye")] == ["goodbye"]
        assert len(cluster.consumers) == 1 and cluster.consumers[0].closed
        await bus.start()  # a restart brings the consumer back
        try:
            assert len(cluster.consumers) == 2 and cluster.consumers[1].started
        finally:
            await bus.stop()


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
