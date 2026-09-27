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
per-aggregate ordering the other Firefly publishers guarantee holds here too.

The consumer is a :class:`~pyfly.messaging.listener_container.KafkaListenerContainer`, shared with the
messaging adapter: auto-commit is off, the matching handlers of one record run in one unit of work the
container opens (their ``@transactional`` joins it), and the offset is committed only after that unit
committed. A handler failure seeks the partition back and attempts the record again after a back-off
(``pyfly.eda.listener.retry.*``); after the last attempt the record is dead-lettered to
``<topic>.DLT`` (and recorded in the :class:`~pyfly.eda.dlq.EdaDeadLetterStore`, when one is given),
then committed. A record the serializer cannot read is dead-lettered at once. ``stop()`` waits for the
record in flight and never commits the offset of one it had to cancel. Delivery is at-least-once.

The adapter requires aiokafka to be installed (``pip install pyfly[kafka]``
or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Callable
from typing import Any

from pyfly.eda.dlq import EdaDeadLetterEntry, EdaDeadLetterStore
from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.adapters.kafka import DLT_BACKOFF_SECONDS, publish_with_retries
from pyfly.messaging.listener_container import (
    DEAD_LETTER_PUBLISH_ATTEMPTS,
    KafkaListenerContainer,
    ListenerContainerSettings,
    PoisonMessageError,
    dead_letter_headers,
    failure_cause,
)

logger = logging.getLogger(__name__)

#: The header LaraFly's ``KafkaEventPublisher`` reads first when choosing a key.
DEFAULT_PARTITION_KEY_HEADER = "partition_key"
#: Suffix of the dead-letter topic a record goes to: ``orders`` -> ``orders.DLT``.
DEFAULT_DLT_SUFFIX = ".DLT"
DLT_PUBLISH_ATTEMPTS = DEAD_LETTER_PUBLISH_ATTEMPTS

__all__ = [
    "DEFAULT_DLT_SUFFIX",
    "DEFAULT_PARTITION_KEY_HEADER",
    "DLT_BACKOFF_SECONDS",
    "DLT_PUBLISH_ATTEMPTS",
    "KafkaEventBus",
    "partition_key",
]


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
        bus instance reads every record, and no offset is committed, so a
        restart delivers nothing again). Set a stable string to share the
        topics across replicas with at-least-once delivery.
    serializer:
        ``EventSerializer`` used to encode and decode envelopes.
        Defaults to ``JsonEventSerializer``.
    partition_key_header:
        The envelope header consulted first for the record key (see
        :func:`partition_key`). Defaults to ``partition_key``.
    dlt_suffix:
        Suffix of the dead-letter topic a record goes to, verbatim, when
        the serializer cannot read it or its handlers failed on every
        attempt. ``None`` switches the dead-letter topic off: such a record
        is logged and skipped (and recorded in *dead_letter_store*, if
        any). Defaults to ``.DLT``.
    settings:
        The listener container settings (``pyfly.eda.listener.*``: retry
        policy, unit of work, shutdown timeout).
    dead_letter_store:
        An :class:`~pyfly.eda.dlq.EdaDeadLetterStore` that also records
        every event whose handlers failed on every attempt, with the error
        and the attempt count.
    consumer_factory / producer_factory:
        Build the aiokafka clients (``AIOKafkaConsumer`` / ``AIOKafkaProducer``
        by default), called with ``bootstrap_servers`` and the bus's settings.
    """

    #: A consumer: it stops before any ``@pre_destroy``, draining the record in flight.
    phase = CONSUMER_PHASE

    def __init__(
        self,
        *,
        bootstrap_servers: str = "localhost:9092",
        topics: list[str] | None = None,
        group: str | None = None,
        serializer: EventSerializer | None = None,
        partition_key_header: str = DEFAULT_PARTITION_KEY_HEADER,
        dlt_suffix: str | None = DEFAULT_DLT_SUFFIX,
        settings: ListenerContainerSettings | None = None,
        dead_letter_store: EdaDeadLetterStore | None = None,
        consumer_factory: Callable[..., Any] | None = None,
        producer_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topics = list(topics) if topics else ["pyfly.events"]
        self._group = group
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._partition_key_header = partition_key_header
        self._dlt_suffix = dlt_suffix
        self._settings = settings or ListenerContainerSettings()
        self._dead_letter_store = dead_letter_store
        self._consumer_factory = consumer_factory
        self._producer_factory = producer_factory
        self._handlers: list[tuple[str, EventHandler]] = []
        self._producer: Any = None
        self._container: KafkaListenerContainer[EventEnvelope] | None = None
        self._started = False
        self._stopping = False
        self._stopped = False
        #: Records sent to a dead-letter topic. Scrape it and alert on it: a silent DLT is the
        #: same failure as a lost message, only later.
        self.dlt_published = 0
        #: Dead-letter publishes that failed after every retry. The record stays uncommitted and is
        #: dead-lettered again, so this counts broker trouble, not lost messages.
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
        :func:`partition_key` over the envelope headers. Until 26.09.06 no key
        was sent at all and Kafka round-robined the records, so two events of
        one aggregate could be consumed in either order.

        A bus that was never started starts on its first publish. While it stops (a handler in flight
        publishing) and after it stopped (a ``@pre_destroy`` publishing), the producer is used, or opened
        again, but the consumer is not started: consumers restart only through :meth:`start`.
        """
        if self._producer is None:
            if self._stopped:
                await self._start_producer()
            else:
                await self.start()
        elif not self._started and not self._stopping and not self._stopped:
            await self.start()

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
        self._stopped = False
        if self._producer is None:
            await self._start_producer()

        # Always attach the consumer — pyfly's ApplicationContext auto-starts adapter beans before
        # application code calls subscribe(), so the consumer cannot wait for handlers. The handler list
        # is read for every record; a record no handler matches is simply committed.
        if self._topics:
            dead_letter = self._dead_letter if self._dlt_suffix is not None or self._dead_letter_store else None
            self._container = KafkaListenerContainer(
                topics=self._topics,
                group=self._group,
                consumer_factory=self._new_consumer,
                convert=self._envelope_of,
                handler=self._dispatch,
                dead_letter=dead_letter,
                settings=self._settings,
                auto_offset_reset="earliest",
                name=f"eda:{','.join(self._topics)}",
                listeners=self._matching,
            )
            await self._container.start()

        self._started = True

    async def stop(self) -> None:
        """Stop the consumer gracefully (the record in flight finishes, or is cancelled without its offset
        being committed), then the producer."""
        self._stopping = True
        try:
            if self._container is not None:
                await self._container.stop()
                self._container = None
        finally:
            self._started = False
            self._stopping = False
            self._stopped = True
            if self._producer is not None:
                await self._producer.stop()
                self._producer = None

    # -- consuming -------------------------------------------------------------------------------------

    async def _start_producer(self) -> None:
        if self._producer_factory is not None:
            producer = self._producer_factory(bootstrap_servers=self._bootstrap_servers)
        else:
            from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

            producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
        await producer.start()
        self._producer = producer

    def _new_consumer(self, **settings: Any) -> Any:
        if self._consumer_factory is not None:
            return self._consumer_factory(bootstrap_servers=self._bootstrap_servers, **settings)
        from aiokafka import AIOKafkaConsumer

        return AIOKafkaConsumer(bootstrap_servers=self._bootstrap_servers, **settings)

    def _envelope_of(self, record: Any, _attempt: int) -> EventEnvelope:
        return self._serializer.deserialize(record.value)

    def _matching(self, envelope: EventEnvelope) -> list[EventHandler]:
        """The handlers whose pattern matches *envelope*'s event type."""
        return [handler for pattern, handler in list(self._handlers) if fnmatch.fnmatch(envelope.event_type, pattern)]

    async def _dispatch(self, envelope: EventEnvelope) -> None:
        """Every matching handler, in the record's unit of work: one failure fails the record."""
        for handler in self._matching(envelope):
            await handler(envelope)

    async def _dead_letter(self, record: Any, error: BaseException, attempts: int) -> None:
        """Republish ``record`` verbatim to ``<topic><dlt_suffix>`` with the reason and origin, and record a
        failed event in the dead-letter store.

        The bytes, key and headers are the original ones — a dead-letter record must be replayable — plus
        ``x-dlt-reason``, ``x-dlt-source-topic``, ``x-dlt-source-partition``, ``x-dlt-source-offset`` and
        ``x-dlt-attempts``, so an operator can find where it came from. The publish is retried with a
        linear back-off; when it still fails the error propagates and the container leaves the record
        uncommitted, to dead-letter it again later.
        """
        if self._dlt_suffix is not None:
            topic = f"{record.topic}{self._dlt_suffix}"
            extra = dead_letter_headers(
                topic=record.topic, error=error, attempts=attempts, partition=record.partition, offset=record.offset
            )
            headers = list(record.headers or []) + [(name, value.encode("utf-8")) for name, value in extra.items()]
            try:
                await publish_with_retries(self._producer, topic, value=record.value, key=record.key, headers=headers)
            except Exception:
                self.dlt_publish_failures += 1
                raise
            self.dlt_published += 1
            logger.warning(
                "record_dead_lettered topic=%s offset=%s dlt=%s reason=%s",
                record.topic,
                record.offset,
                topic,
                type(failure_cause(error)).__name__,
            )
        if self._dead_letter_store is not None and not isinstance(error, PoisonMessageError):
            cause = failure_cause(error)
            await self._dead_letter_store.add(
                EdaDeadLetterEntry(
                    event=self._serializer.deserialize(record.value),
                    error_type=type(cause).__name__,
                    error_message=str(cause),
                    attempts=attempts,
                )
            )
