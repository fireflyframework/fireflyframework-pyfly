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

Every record is produced with a partition key (see :func:`partition_key`) so the
per-aggregate ordering the other Firefly publishers guarantee holds here too,
and a record the serializer cannot read is dead-lettered to ``<topic>.DLT``
before the loop moves on, so auto-commit never advances past a message nobody
has seen.

The adapter requires aiokafka to be installed (``pip install pyfly[kafka]``
or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer

logger = logging.getLogger(__name__)

#: The header LaraFly's ``KafkaEventPublisher`` reads first when choosing a key.
DEFAULT_PARTITION_KEY_HEADER = "partition_key"
#: Suffix of the dead-letter topic a poison record goes to: ``orders`` -> ``orders.DLT``.
DEFAULT_DLT_SUFFIX = ".DLT"
DLT_PUBLISH_ATTEMPTS = 3
DLT_BACKOFF_SECONDS = 0.5


def partition_key(
    event_type: str,
    headers: dict[str, str] | None,
    *,
    header: str = DEFAULT_PARTITION_KEY_HEADER,
) -> str:
    """The key a record is produced with — LaraFly's ``KafkaEventPublisher::partitionKey`` rule.

    ``headers[header] ?? headers['x-correlation-id'] ?? event_type``. The PHP ``??`` falls
    through on an absent key only, so an empty-string header is kept as the key, matching PHP
    byte for byte: the two runtimes must key the same event the same way or per-aggregate
    ordering is lost the moment a topic is written from both sides.
    """
    headers = headers or {}
    if header in headers:
        return headers[header]
    if "x-correlation-id" in headers:
        return headers["x-correlation-id"]
    return event_type


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
    partition_key_header:
        The envelope header consulted first for the record key (see
        :func:`partition_key`). Defaults to ``partition_key``.
    dlt_suffix:
        Suffix of the dead-letter topic an undeserialisable record is
        republished to, verbatim, before the consume loop moves on.
        ``None`` switches dead-lettering off and restores log-and-skip.
        Defaults to ``.DLT``.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str = "localhost:9092",
        topics: list[str] | None = None,
        group: str | None = None,
        serializer: EventSerializer | None = None,
        partition_key_header: str = DEFAULT_PARTITION_KEY_HEADER,
        dlt_suffix: str | None = DEFAULT_DLT_SUFFIX,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topics = list(topics) if topics else ["pyfly.events"]
        self._group = group
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._partition_key_header = partition_key_header
        self._dlt_suffix = dlt_suffix
        self._handlers: list[tuple[str, EventHandler]] = []
        self._producer: Any = None
        self._consumer: Any = None
        self._consume_task: asyncio.Task[None] | None = None
        self._started = False
        #: Records sent to a dead-letter topic. Scrape it and alert on it: a silent DLT is the
        #: same failure as a lost message, only later.
        self.dlt_published = 0
        #: Records lost because even the dead-letter publish failed after every retry.
        self.dlt_publish_failures = 0

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        self._handlers.append((event_type_pattern, handler))

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        *,
        key: str | None = None,
    ) -> None:
        """Produce one keyed record.

        ``key`` overrides the derived key for the one call; otherwise it is
        :func:`partition_key` over the envelope headers. Until 26.09.05 no key
        was sent at all and Kafka round-robined the records, so two events of
        one aggregate could be consumed in either order.
        """
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
        record_key = (
            key if key is not None else partition_key(event_type, envelope.headers, header=self._partition_key_header)
        )
        await self._producer.send_and_wait(
            destination,
            value=self._serializer.serialize(envelope),
            key=record_key.encode("utf-8"),
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
            with contextlib.suppress(asyncio.CancelledError):
                await self._consume_task
            self._consume_task = None
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def _dead_letter(self, record: Any, reason: str) -> None:
        """Republish ``record`` verbatim to ``<topic><dlt_suffix>`` with the reason and origin.

        The bytes, key and headers are the original ones — a dead-letter record must be
        replayable — plus ``x-dlt-reason``, ``x-dlt-source-topic`` and ``x-dlt-source-offset`` so
        an operator can find where it came from. The publish is retried with a linear back-off
        because the most likely cause of a failed DLT publish is the broker being briefly away,
        and after the last attempt the loss is logged CRITICAL and counted: at that point the
        message is gone and the only honest thing left is to say so loudly.
        """
        topic = f"{record.topic}{self._dlt_suffix}"
        headers = list(record.headers or [])
        headers.append(("x-dlt-reason", reason.encode("utf-8")))
        headers.append(("x-dlt-source-topic", str(record.topic).encode("utf-8")))
        headers.append(("x-dlt-source-offset", str(record.offset).encode("utf-8")))

        last_error: Exception | None = None
        for attempt in range(1, DLT_PUBLISH_ATTEMPTS + 1):
            try:
                await self._producer.send_and_wait(topic, value=record.value, key=record.key, headers=headers)
            except Exception as exc:
                last_error = exc
                if attempt < DLT_PUBLISH_ATTEMPTS:
                    await asyncio.sleep(DLT_BACKOFF_SECONDS * attempt)
                continue
            self.dlt_published += 1
            logger.warning(
                "record_dead_lettered topic=%s offset=%s dlt=%s reason=%s",
                record.topic,
                record.offset,
                topic,
                reason,
            )
            return

        self.dlt_publish_failures += 1
        logger.critical(
            "dead_letter_publish_failed topic=%s offset=%s dlt=%s error=%s — the message is lost",
            record.topic,
            record.offset,
            topic,
            last_error,
        )

    async def _consume_loop(self) -> None:
        try:
            async for record in self._consumer:
                try:
                    envelope = self._serializer.deserialize(record.value)
                except Exception as exc:
                    # With auto-commit on, the offset advances whether or not anyone read this
                    # record, so it must be dead-lettered BEFORE the loop moves on — a decorator
                    # on a listener cannot help, no listener ever sees it. Handler failures are
                    # deliberately not dead-lettered here: the envelope was readable, and what
                    # to do with a failing handler (retry, DLT by event id, fail fast) is the
                    # listener's error strategy, not the transport's.
                    if self._dlt_suffix is not None:
                        await self._dead_letter(record, reason=type(exc).__name__)
                    else:
                        logger.exception(
                            "Failed to deserialize record from topic=%s offset=%s",
                            record.topic,
                            record.offset,
                        )
                    continue
                for pattern, handler in self._handlers:
                    if fnmatch.fnmatch(envelope.event_type, pattern):
                        try:
                            await handler(envelope)
                        except Exception:
                            logger.exception(
                                "Handler for pattern=%s raised on event_type=%s",
                                pattern,
                                envelope.event_type,
                            )
        except asyncio.CancelledError:
            pass
