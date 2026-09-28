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
"""Helpers for the outbox forwarding tests on real brokers: a :class:`Broker` lane (a Kafka or RabbitMQ
testcontainer, with a transport to publish with and the messages the broker holds, read back directly), and
:func:`eventually`, which waits for a condition.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.serializers import JsonEventSerializer
from pyfly.eda.types import EventEnvelope


@dataclass
class Broker:
    """One broker lane: a transport to publish with, and the messages the broker holds for the test."""

    name: str
    url: str
    destination: str = field(default_factory=lambda: f"wp09b.{uuid.uuid4().hex[:10]}")
    queues: list[str] = field(default_factory=list)
    #: The messages taken off the test's RabbitMQ queue so far (a Kafka topic is read again from its start).
    taken: list[EventEnvelope] = field(default_factory=list)

    def transport(self, url: str | None = None) -> EventPublisher:
        """A transport of its own (a process's bus) on the broker, or on *url* (the same broker behind a proxy)."""
        group = f"wp09b-{uuid.uuid4().hex[:8]}"
        if self.name == "kafka":
            from pyfly.eda.adapters.kafka import KafkaEventBus

            return KafkaEventBus(bootstrap_servers=url or self.url, topics=[self.destination], group=group)
        from pyfly.eda.adapters.rabbitmq import RabbitMqEventBus

        self.queues.append(f"{group}.{self.destination}")
        return RabbitMqEventBus(url=url or self.url, destinations=[self.destination], group=group)

    async def listen(self) -> None:
        """Before anything is published: bind a queue of the test's own to the destination (RabbitMQ drops what
        no queue is bound for; a Kafka topic keeps everything)."""
        if self.name != "rabbitmq":
            return
        import aio_pika

        queue = f"wp09b-test.{self.destination}"
        self.queues.append(queue)
        connection = await aio_pika.connect_robust(self.url)
        try:
            channel = await connection.channel()
            exchange = await channel.declare_exchange("pyfly", aio_pika.ExchangeType.DIRECT, durable=True)
            declared = await channel.declare_queue(queue, durable=True)
            await declared.bind(exchange, routing_key=self.destination)
        finally:
            await connection.close()

    async def messages(self, *, expected: int, settle: float = 1.5, timeout: float = 30.0) -> list[EventEnvelope]:
        """Every message the broker received for the destination since the test began: read until *expected*
        arrived (or *timeout*), then for *settle* seconds more, so a message too many is seen too."""
        if self.name == "kafka":
            return await self._kafka_messages(expected, settle, timeout)
        return await self._rabbit_messages(expected, settle, timeout)

    async def _kafka_messages(self, expected: int, settle: float, timeout: float) -> list[EventEnvelope]:
        from aiokafka import AIOKafkaConsumer  # type: ignore[import-untyped]

        consumer = AIOKafkaConsumer(self.destination, bootstrap_servers=self.url, auto_offset_reset="earliest")
        await consumer.start()
        envelopes: list[EventEnvelope] = []
        try:
            deadline = time.monotonic() + timeout
            quiet_until: float | None = None
            while time.monotonic() < deadline:
                batch = await consumer.getmany(timeout_ms=300)
                for records in batch.values():
                    envelopes += [JsonEventSerializer().deserialize(record.value) for record in records]
                if len(envelopes) >= expected:
                    quiet_until = quiet_until or time.monotonic() + settle
                    if time.monotonic() >= quiet_until:
                        break
        finally:
            await consumer.stop()
        return envelopes

    async def _rabbit_messages(self, expected: int, settle: float, timeout: float) -> list[EventEnvelope]:
        import aio_pika

        connection = await aio_pika.connect_robust(self.url)
        envelopes = self.taken
        try:
            channel = await connection.channel()
            queue = await channel.declare_queue(f"wp09b-test.{self.destination}", durable=True, passive=True)
            deadline = time.monotonic() + timeout
            quiet_until: float | None = None
            while time.monotonic() < deadline:
                message = await queue.get(no_ack=True, fail=False)
                if message is not None:
                    envelopes.append(JsonEventSerializer().deserialize(message.body))
                    continue
                if len(envelopes) >= expected:
                    quiet_until = quiet_until or time.monotonic() + settle
                    if time.monotonic() >= quiet_until:
                        break
                await asyncio.sleep(0.1)
        finally:
            await connection.close()
        return list(envelopes)

    async def cleanup(self) -> None:
        if self.name != "rabbitmq" or not self.queues:
            return
        import aio_pika

        connection = await aio_pika.connect_robust(self.url)
        try:
            channel = await connection.channel()
            for queue in self.queues:
                with contextlib.suppress(Exception):
                    await channel.queue_delete(queue)
        finally:
            await connection.close()


async def eventually(condition: Callable[[], object], timeout: float = 30.0) -> None:
    """Wait until *condition()* is true, checking every 50 ms; ``AssertionError`` after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")
