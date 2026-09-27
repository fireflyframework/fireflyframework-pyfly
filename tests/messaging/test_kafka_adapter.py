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

    def test_is_a_consumer_phase_bean_whose_container_handles_errors(self) -> None:
        from pyfly.kernel.lifecycle import CONSUMER_PHASE, lifecycle_phase

        adapter = KafkaAdapter(bootstrap_servers="localhost:9092")
        assert lifecycle_phase(adapter) == CONSUMER_PHASE
        assert adapter.manages_listener_errors is True

    @pytest.mark.asyncio
    async def test_consumers_run_with_auto_commit_off(self) -> None:
        from tests.messaging.brokers import FakeKafkaCluster

        cluster = FakeKafkaCluster()
        adapter = KafkaAdapter(
            bootstrap_servers="fake:9092", consumer_factory=cluster.consumer, producer_factory=cluster.producer
        )

        async def handler(msg):
            pass

        await adapter.subscribe("orders", handler, group="g1")
        await adapter.start()
        try:
            [consumer] = cluster.consumers
            assert consumer.enable_auto_commit is False
            assert consumer.group_id == "g1"
            assert consumer.topics == ["orders"]
        finally:
            await adapter.stop()
        assert consumer.closed

    @pytest.mark.asyncio
    async def test_a_late_subscription_joins_the_running_consumer_of_its_topic_and_group(self) -> None:
        from tests.messaging.brokers import FakeKafkaCluster
        from tests.messaging.listener_app import eventually

        cluster = FakeKafkaCluster()
        adapter = KafkaAdapter(
            bootstrap_servers="fake:9092",
            consumer_factory=cluster.consumer,
            producer_factory=cluster.producer,
            auto_offset_reset="earliest",
        )
        seen: list[str] = []

        async def first(msg):
            seen.append("first")

        async def second(msg):
            seen.append("second")

        await adapter.subscribe("orders", first, group="g1")
        await adapter.start()
        await adapter.subscribe("orders", second, group="g1")
        try:
            await adapter.publish("orders", b"x")
            await eventually(lambda: seen == ["first", "second"], what="both handlers")
        finally:
            await adapter.stop()
        assert len(cluster.consumers) == 1  # one consumer: two would split the partitions between them
