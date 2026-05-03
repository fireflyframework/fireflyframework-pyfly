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
"""Kafka message broker adapter — wraps aiokafka."""

from __future__ import annotations

import asyncio
from typing import Any

from pyfly.messaging.ports.outbound import MessageHandler
from pyfly.messaging.types import Message


class KafkaAdapter:
    """MessageBrokerPort implementation backed by Apache Kafka via aiokafka.

    Requires aiokafka to be installed (install the kafka extra: pyfly[kafka]).
    """

    def __init__(self, bootstrap_servers: str = "localhost:9092") -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: Any = None
        self._consumers: list[Any] = []
        self._handlers: list[tuple[str, MessageHandler, str | None]] = []
        self._consumer_tasks: list[asyncio.Task[None]] = []

    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        kafka_headers = [(k, v.encode()) for k, v in headers.items()] if headers else None
        await self._producer.send_and_wait(topic, value=value, key=key, headers=kafka_headers)

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        group: str | None = None,
    ) -> None:
        self._handlers.append((topic, handler, group))

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore[import-untyped]

        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
        await self._producer.start()

        grouped: dict[tuple[str, str | None], list[tuple[str, MessageHandler]]] = {}
        for topic, handler, group in self._handlers:
            key = (topic, group)
            grouped.setdefault(key, []).append((topic, handler))

        for (topic, group), entries in grouped.items():
            consumer = AIOKafkaConsumer(
                topic,
                bootstrap_servers=self._bootstrap_servers,
                group_id=group,
            )
            await consumer.start()
            self._consumers.append(consumer)
            handlers_for_consumer = [h for _, h in entries]
            task = asyncio.create_task(self._consume_loop(consumer, handlers_for_consumer))
            self._consumer_tasks.append(task)

    async def stop(self) -> None:
        for task in self._consumer_tasks:
            task.cancel()
        if self._consumer_tasks:
            await asyncio.gather(*self._consumer_tasks, return_exceptions=True)
        for consumer in self._consumers:
            await consumer.stop()
        if self._producer is not None:
            await self._producer.stop()

    async def _consume_loop(self, consumer: Any, handlers: list[MessageHandler]) -> None:
        try:
            async for record in consumer:
                headers = {}
                if record.headers:
                    for k, v in record.headers:
                        try:
                            headers[k] = v.decode()
                        except (UnicodeDecodeError, AttributeError):
                            headers[k] = v.hex() if isinstance(v, bytes) else str(v)
                msg = Message(
                    topic=record.topic,
                    value=record.value,
                    key=record.key,
                    headers=headers,
                )
                for handler in handlers:
                    await handler(msg)
        except asyncio.CancelledError:
            pass
