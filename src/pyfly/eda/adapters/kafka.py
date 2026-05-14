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
"""Kafka-backed ``EventPublisher`` — wraps aiokafka.

``destination`` maps to a Kafka topic. Each subscriber registers an
``event_type`` pattern (``fnmatch`` style) against a fixed list of topics
the bus is configured to consume from; on every record the bus
deserialises the envelope and dispatches to every handler whose
pattern matches ``envelope.event_type``.

The adapter requires aiokafka to be installed (``pip install pyfly[kafka]``
or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer

logger = logging.getLogger(__name__)


class KafkaEventBus:
    """``EventPublisher`` backed by Apache Kafka.

    Parameters
    ----------
    bootstrap_servers:
        Comma-separated ``host:port`` list for the producer and consumer.
    topics:
        Topics the consumer subscribes to. Subscribers register
        ``event_type`` patterns; the bus deserialises the envelope and
        dispatches to any matching handler. Defaults to ``["pyfly.events"]``.
    group:
        Kafka consumer group. ``None`` means an isolated consumer (each
        bus instance reads every record). Set to a stable string when
        you want at-most-once delivery across replicas.
    serializer:
        ``EventSerializer`` used to encode and decode envelopes.
        Defaults to ``JsonEventSerializer``.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str = "localhost:9092",
        topics: list[str] | None = None,
        group: str | None = None,
        serializer: EventSerializer | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topics = list(topics) if topics else ["pyfly.events"]
        self._group = group
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._handlers: list[tuple[str, EventHandler]] = []
        self._producer: Any = None
        self._consumer: Any = None
        self._consume_task: asyncio.Task[None] | None = None
        self._started = False

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        self._handlers.append((event_type_pattern, handler))

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        if not self._started:
            await self.start()
        from pyfly.eda.types import EventEnvelope

        envelope = EventEnvelope(
            event_type=event_type,
            payload=payload,
            destination=destination,
            headers=headers or {},
        )
        record_headers = [(k, v.encode()) for k, v in envelope.headers.items()]
        await self._producer.send_and_wait(
            destination,
            value=self._serializer.serialize(envelope),
            headers=record_headers or None,
        )

    async def start(self) -> None:
        if self._started:
            return
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore[import-untyped]

        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
        await self._producer.start()

        # Always attach the consumer — pyfly's ApplicationContext auto-
        # starts adapter beans before application code calls subscribe(),
        # so we cannot gate the consumer on handlers being present yet.
        # _consume_loop iterates _handlers per-message; an empty list
        # means messages are received-and-dropped (with auto-commit) but
        # that's expected behaviour when no subscribers exist.
        if self._topics:
            self._consumer = AIOKafkaConsumer(
                *self._topics,
                bootstrap_servers=self._bootstrap_servers,
                group_id=self._group,
                enable_auto_commit=True,
                auto_offset_reset="earliest",
            )
            await self._consumer.start()
            self._consume_task = asyncio.create_task(self._consume_loop())

        self._started = True

    async def stop(self) -> None:
        self._started = False
        if self._consume_task is not None:
            self._consume_task.cancel()
            try:
                await self._consume_task
            except asyncio.CancelledError:
                pass
            self._consume_task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def _consume_loop(self) -> None:
        try:
            async for record in self._consumer:
                try:
                    envelope = self._serializer.deserialize(record.value)
                except Exception:
                    logger.exception(
                        "Failed to deserialize record from topic=%s offset=%s",
                        record.topic, record.offset,
                    )
                    continue
                for pattern, handler in self._handlers:
                    if fnmatch.fnmatch(envelope.event_type, pattern):
                        try:
                            await handler(envelope)
                        except Exception:
                            logger.exception(
                                "Handler for pattern=%s raised on event_type=%s",
                                pattern, envelope.event_type,
                            )
        except asyncio.CancelledError:
            pass
