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
"""Kafka message broker adapter — wraps aiokafka.

Each (topic, group) pair is consumed by one
:class:`~pyfly.messaging.listener_container.KafkaListenerContainer`: auto-commit is off, every record
runs in a unit of work the container opens with the listeners' ``@transactional`` settings, and its
offset is committed only after that unit committed.
A failed record is sought back and attempted again after a back-off, then published to its dead-letter
topic (``<topic>.DLT`` unless the listener names another) and committed. ``stop()`` waits for the record
in flight and never commits the offset of one it had to cancel.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any

from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.listener_container import (
    DEAD_LETTER_PUBLISH_ATTEMPTS,
    KafkaListenerContainer,
    ListenerContainerSettings,
    ListenerOptions,
    dead_letter_headers,
    listener_options,
)
from pyfly.messaging.ports.outbound import MessageHandler
from pyfly.messaging.types import Message

logger = logging.getLogger(__name__)

#: Suffix of the dead-letter topic a record goes to after its last attempt: ``orders`` -> ``orders.DLT``.
DEFAULT_DLT_SUFFIX = ".DLT"
DLT_BACKOFF_SECONDS = 0.5


def record_headers(record: Any) -> dict[str, str]:
    """A record's headers as text; a value that is not UTF-8 becomes its hex form."""
    headers: dict[str, str] = {}
    for name, raw in record.headers or ():
        try:
            headers[name] = raw.decode()
        except (UnicodeDecodeError, AttributeError):
            headers[name] = raw.hex() if isinstance(raw, bytes) else str(raw)
    return headers


def message_of(record: Any, attempt: int) -> Message:
    """The :class:`Message` a handler receives for *record* on its *attempt*-th delivery."""
    return Message(
        topic=record.topic,
        value=record.value,
        key=record.key,
        headers=record_headers(record),
        partition=record.partition,
        offset=record.offset,
        delivery_attempt=attempt,
    )


class KafkaAdapter:
    """MessageBrokerPort implementation backed by Apache Kafka via aiokafka.

    Requires aiokafka to be installed (install the kafka extra: pyfly[kafka]).

    Parameters
    ----------
    bootstrap_servers:
        Comma-separated ``host:port`` list for the producer and the consumers.
    settings:
        The listener container settings (retry policy, unit of work, shutdown timeout); by default
        :class:`~pyfly.messaging.listener_container.ListenerContainerSettings`.
    auto_offset_reset:
        Where a group with no committed offset starts: ``latest`` (Kafka's default) or ``earliest``.
    dead_letter_suffix:
        Suffix of the dead-letter topic a record goes to after its last attempt. ``None`` switches
        dead-lettering off: the record is logged and skipped after its last attempt.
    consumer_factory / producer_factory:
        Build the aiokafka clients (``AIOKafkaConsumer`` / ``AIOKafkaProducer`` by default), called with
        ``bootstrap_servers`` and the adapter's settings; pass ``functools.partial(AIOKafkaConsumer,
        security_protocol="SSL", ...)`` to add client settings.
    """

    #: A consumer: it stops before any ``@pre_destroy``, draining the records in flight.
    phase = CONSUMER_PHASE
    #: The listener container retries and dead-letters deliveries (see ``wrap_listener``).
    manages_listener_errors = True

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        *,
        settings: ListenerContainerSettings | None = None,
        auto_offset_reset: str = "latest",
        dead_letter_suffix: str | None = DEFAULT_DLT_SUFFIX,
        consumer_factory: Callable[..., Any] | None = None,
        producer_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._settings = settings or ListenerContainerSettings()
        self._auto_offset_reset = auto_offset_reset
        self._dead_letter_suffix = dead_letter_suffix
        self._consumer_factory = consumer_factory
        self._producer_factory = producer_factory
        self._producer: Any = None
        self._containers: dict[tuple[str, str | None], KafkaListenerContainer[Message]] = {}
        self._container_options: dict[tuple[str, str | None], ListenerOptions | None] = {}
        self._dispatch: dict[tuple[str, str | None], list[MessageHandler]] = {}
        self._started = False

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
        """Register *handler* for *topic* in *group*.

        The handlers of one (topic, group) share one consumer, and each record goes to all of them in one
        unit of work, when their ``@transactional`` settings let them share one (a WARNING names them when
        they do not; see :meth:`~pyfly.messaging.listener_container.ListenerInvoker.plan`). PyFly's
        ApplicationContext starts adapter beans BEFORE ``@message_listener`` wiring calls ``subscribe()``,
        so a subscription that arrives after ``start()`` joins the running consumer of its (topic, group),
        or starts one.
        """
        key = (topic, group)
        self._dispatch.setdefault(key, []).append(handler)
        if not self._started:
            return
        container = self._containers.get(key)
        if container is None:
            await self._start_container(topic, group)
            return
        options = listener_options(handler)
        if options is not None and options != self._container_options.get(key):
            self._warn_options_differ(key)
        container.check_listeners(self._dispatch[key])

    async def start(self) -> None:
        if self._started:
            return
        self._producer = self._new_producer()
        await self._producer.start()
        self._started = True
        for topic, group in list(self._dispatch):
            if (topic, group) not in self._containers:
                await self._start_container(topic, group)

    async def stop(self) -> None:
        """Stop every consumer gracefully (in parallel), then the producer: an in-flight handler may still
        publish while it finishes."""
        self._started = False
        containers = list(self._containers.values())
        self._containers.clear()
        results = await asyncio.gather(*(container.stop() for container in containers), return_exceptions=True)
        for container, result in zip(containers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("kafka_listener_stop_failed container=%s: %s", container.name, result)
        if self._producer is not None:
            await self._producer.stop()

    # -- consumers ---------------------------------------------------------------------------------------

    def _new_producer(self) -> Any:
        if self._producer_factory is not None:
            return self._producer_factory(bootstrap_servers=self._bootstrap_servers)
        from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

        return AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)

    def _new_consumer(self, **settings: Any) -> Any:
        if self._consumer_factory is not None:
            return self._consumer_factory(bootstrap_servers=self._bootstrap_servers, **settings)
        from aiokafka import AIOKafkaConsumer

        return AIOKafkaConsumer(bootstrap_servers=self._bootstrap_servers, **settings)

    def _options(self, key: tuple[str, str | None]) -> ListenerOptions | None:
        declared = [options for options in map(listener_options, self._dispatch[key]) if options is not None]
        if len(set(declared)) > 1:
            self._warn_options_differ(key)
        return declared[0] if declared else None

    @staticmethod
    def _warn_options_differ(key: tuple[str, str | None]) -> None:
        logger.warning(
            "kafka_listener_options_differ topic=%s group=%s: the listeners of one (topic, group) share a "
            "consumer; the first one's retries and dead-letter topic apply",
            *key,
        )

    async def _start_container(self, topic: str, group: str | None) -> None:
        key = (topic, group)
        options = self._options(key)
        self._container_options[key] = options
        dead_letter_topic = (options.dead_letter if options is not None else None) or (
            f"{topic}{self._dead_letter_suffix}" if self._dead_letter_suffix is not None else None
        )
        container: KafkaListenerContainer[Message] = KafkaListenerContainer(
            topics=[topic],
            group=group,
            consumer_factory=self._new_consumer,
            convert=message_of,
            handler=functools.partial(self._deliver, key),
            dead_letter=(
                functools.partial(self._dead_letter, dead_letter_topic) if dead_letter_topic is not None else None
            ),
            settings=self._settings,
            retry=self._settings.retry.with_options(options),
            auto_offset_reset=self._auto_offset_reset,
            name=f"{topic}[{group or '-'}]",
            listeners=functools.partial(self._listeners, key),
        )
        container.check_listeners(self._dispatch[key])
        self._containers[key] = container
        try:
            await container.start()
        except BaseException:
            self._containers.pop(key, None)
            raise

    def _listeners(self, key: tuple[str, str | None], _message: Message) -> list[MessageHandler]:
        """The handlers a record of the (topic, group) goes to."""
        return self._dispatch.get(key, [])

    async def _deliver(self, key: tuple[str, str | None], message: Message) -> None:
        """Every handler of the (topic, group), in the delivery's unit of work: one failure fails them all."""
        for handler in list(self._dispatch.get(key, ())):
            await handler(message)

    async def _dead_letter(self, topic: str, record: Any, error: BaseException, attempts: int) -> None:
        """Publish *record* to *topic* with its bytes, key and headers, plus where and why it failed."""
        extra = dead_letter_headers(
            topic=record.topic, error=error, attempts=attempts, partition=record.partition, offset=record.offset
        )
        headers = list(record.headers or ()) + [(name, value.encode()) for name, value in extra.items()]
        await publish_with_retries(self._producer, topic, value=record.value, key=record.key, headers=headers)


async def publish_with_retries(producer: Any, topic: str, *, value: Any, key: Any, headers: list[Any]) -> None:
    """Send one record, retried with a linear back-off: the likeliest cause of a failed dead-letter publish
    is the broker being briefly away. The last failure is raised, so the source record stays uncommitted."""
    for attempt in range(1, DEAD_LETTER_PUBLISH_ATTEMPTS + 1):
        try:
            await producer.send_and_wait(topic, value=value, key=key, headers=headers)
            return
        except Exception:
            if attempt == DEAD_LETTER_PUBLISH_ATTEMPTS:
                raise
            await asyncio.sleep(DLT_BACKOFF_SECONDS * attempt)
