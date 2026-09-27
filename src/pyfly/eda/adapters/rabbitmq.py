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
"""RabbitMQ-backed ``EventPublisher`` — wraps aio-pika.

``destination`` maps to a RabbitMQ routing key (and queue name prefix).
Each subscriber registers an ``event_type`` pattern (``fnmatch`` style)
against a fixed list of destinations the bus is configured to consume
from; on every message the bus deserialises the envelope and dispatches
to every handler whose pattern matches ``envelope.event_type``.

Each destination's queue is consumed by a
:class:`~pyfly.messaging.listener_container.RabbitListenerContainer`, shared with the messaging adapter:
a channel of its own with a bounded prefetch (``pyfly.eda.rabbitmq.prefetch``), manual acknowledgement,
and the matching handlers of one message in one unit of work the container opens with their
``@transactional`` settings, acked only after it committed. A handler failure is attempted again after a
back-off (``pyfly.eda.listener.retry.*``), by republishing the message to its queue with its attempt
count; after the last attempt, and at once for a message the serializer cannot read, it is dead-lettered
to ``<exchange>.dlx`` into the durable queue ``<group>.<destination>.dlq``. The bus's consumers share a
concurrency limit sized from the datasource pool. Before 26.09.08 a failing handler was requeued at once,
forever, with every message of the backlog running at the same time.

The queues are declared when the bus starts, so events published from then on wait in them, but
consuming begins only once a handler has subscribed: the application context starts the bus before it
subscribes the ``@event_listener`` methods, and a message delivered in between would match no handler
and be acked, lost.

The adapter requires aio-pika to be installed (``pip install pyfly[rabbitmq]``
or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pyfly.eda.dlq import EdaDeadLetterEntry, EdaDeadLetterStore
from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer
from pyfly.eda.types import EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.listener_container import (
    ConcurrencyLimit,
    ListenerContainerSettings,
    ListenerInvoker,
    PoisonMessageError,
    RabbitDeadLetter,
    RabbitListenerContainer,
    failure_cause,
)

logger = logging.getLogger(__name__)


class RabbitMqEventBus:
    """``EventPublisher`` backed by RabbitMQ via aio-pika.

    Parameters
    ----------
    url:
        AMQP connection URL. Defaults to ``amqp://guest:guest@localhost/``.
    exchange_name:
        Name of the DIRECT exchange to declare. Defaults to ``pyfly``.
    destinations:
        Routing keys the consumer binds to. Each destination gets a durable
        queue named ``<group>.<destination>`` bound with that routing key.
        Defaults to ``["pyfly.events"]``.
    group:
        Consumer group prefix used in queue names. Defaults to
        ``pyfly-default``.
    serializer:
        ``EventSerializer`` used to encode and decode envelopes.
        Defaults to ``JsonEventSerializer``.
    settings:
        The listener container settings (``pyfly.eda.listener.*``: retry
        policy, unit of work, prefetch, concurrency, shutdown timeout).
    dead_letter_exchange:
        The exchange an event goes to after its last attempt, with its
        queue name as the routing key, into ``<queue>.dlq``. Defaults to
        ``<exchange_name>.dlx``.
    dead_letter_store:
        An :class:`~pyfly.eda.dlq.EdaDeadLetterStore` that also records
        every event whose handlers failed on every attempt. The event is
        in the dead-letter queue first, so a failure to record it is logged
        and counted (``dead_letter_store_failures``), not retried.
    connection_factory:
        Opens the connection (``aio_pika.connect_robust`` by default).
    """

    #: A consumer: it stops before any ``@pre_destroy``, draining the deliveries in flight.
    phase = CONSUMER_PHASE

    def __init__(
        self,
        *,
        url: str = "amqp://guest:guest@localhost/",
        exchange_name: str = "pyfly",
        destinations: list[str] | None = None,
        group: str = "pyfly-default",
        serializer: EventSerializer | None = None,
        settings: ListenerContainerSettings | None = None,
        dead_letter_exchange: str | None = None,
        dead_letter_store: EdaDeadLetterStore | None = None,
        connection_factory: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._destinations = list(destinations) if destinations else ["pyfly.events"]
        self._group = group
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._settings = settings or ListenerContainerSettings()
        self._dead_letter_exchange = dead_letter_exchange or f"{exchange_name}.dlx"
        self._dead_letter_store = dead_letter_store
        self._connection_factory = connection_factory
        self._handlers: list[tuple[str, EventHandler]] = []
        #: Set once a handler subscribed: the consumers begin consuming then.
        self._listening = asyncio.Event()
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._containers: list[RabbitListenerContainer[EventEnvelope]] = []
        invoker = ListenerInvoker(self._settings, name=f"eda rabbitmq {exchange_name}")
        settings_ = self._settings
        self._limit = ConcurrencyLimit(
            lambda: settings_.concurrency or invoker.suggested_concurrency(settings_.prefetch)
        )
        self._started = False
        self._stopping = False
        self._stopped = False
        #: Events in the dead-letter queue that the dead-letter store failed to record.
        self.dead_letter_store_failures = 0

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        """Register a handler for events matching *event_type_pattern*.

        Handlers may be registered before or after :meth:`start`: each running
        consumer reads the current handler list on every message, so a handler
        added after start begins receiving matching events immediately — no
        restart and no extra consumer. The consumers begin consuming once the
        first handler subscribed; the ones subscribed in the same step land
        before the first delivery.
        """
        self._handlers.append((event_type_pattern, handler))
        self._listening.set()

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish an event to *destination* on the exchange.

        A bus that was never started starts on its first publish. While it stops (a handler in flight
        publishing) its publishing channel is used; after it stopped (a ``@pre_destroy`` publishing) each
        publish opens a connection of its own and closes it again. Neither starts a consumer: consumers
        restart only through :meth:`start`.
        """
        if self._exchange is None:
            if not self._stopped:
                await self.start()
        elif not self._started and not self._stopping and not self._stopped:
            await self.start()
        import aio_pika

        envelope = EventEnvelope(
            event_type=event_type,
            payload=payload,
            destination=destination,
            headers=headers or {},
        )
        message = aio_pika.Message(
            body=self._serializer.serialize(envelope),
            headers=headers or {},  # type: ignore[arg-type]
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=envelope.event_id,
        )
        if self._exchange is not None:
            await self._exchange.publish(message, routing_key=destination)
            return
        connection, _channel, exchange = await self._connect()  # stopped: nothing would close it later
        try:
            await exchange.publish(message, routing_key=destination)
        finally:
            with contextlib.suppress(Exception):
                await connection.close()

    async def start(self) -> None:
        """Connect to RabbitMQ and begin consuming from all destinations."""
        if self._started:
            return
        self._stopped = False
        try:
            if self._exchange is None:
                await self._open_publisher()
            for destination in self._destinations:
                await self._start_consumer(destination)
        except Exception:
            # Don't leak a half-open connection if a consumer/exchange declare fails.
            await self._stop_consumers()
            if self._connection is not None:
                with contextlib.suppress(Exception):
                    await self._connection.close()
            self._connection = self._channel = self._exchange = None
            raise

        self._started = True

    async def _open_publisher(self) -> None:
        self._connection, self._channel, self._exchange = await self._connect()

    async def _connect(self) -> tuple[Any, Any, Any]:
        """A connection, its publishing channel and the declared exchange."""
        import aio_pika

        connect = self._connection_factory or aio_pika.connect_robust
        connection = await connect(self._url)
        try:
            channel = await connection.channel()
            exchange = await channel.declare_exchange(self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True)
        except BaseException:
            with contextlib.suppress(Exception):
                await connection.close()
            raise
        return connection, channel, exchange

    async def _start_consumer(self, destination: str) -> None:
        """Consume ``<group>.<destination>``, bound to *destination*, through a listener container."""
        queue = f"{self._group}.{destination}"
        container: RabbitListenerContainer[EventEnvelope] = RabbitListenerContainer(
            connection=self._connection,
            queue=queue,
            bindings=[(self._exchange_name, destination)],
            convert=self._envelope_of,
            handler=self._dispatch,
            dead_letters=[RabbitDeadLetter(self._dead_letter_exchange, queue, f"{queue}.dlq")],
            settings=self._settings,
            limit=self._limit,
            after_dead_letter=self._record_dead_letter if self._dead_letter_store is not None else None,
            name=f"eda:{queue}",
            listeners=self._matching,
            ready=self._listening,
        )
        self._containers.append(container)
        await container.start()

    def _envelope_of(self, message: Any, _attempt: int) -> EventEnvelope:
        return self._serializer.deserialize(message.body)

    def _matching(self, envelope: EventEnvelope) -> list[EventHandler]:
        """The handlers whose pattern matches *envelope*'s event type."""
        return [handler for pattern, handler in list(self._handlers) if fnmatch.fnmatch(envelope.event_type, pattern)]

    async def _dispatch(self, envelope: EventEnvelope) -> None:
        """Every matching handler, in the delivery's unit of work: one failure fails the delivery."""
        for handler in self._matching(envelope):
            await handler(envelope)

    async def _record_dead_letter(self, message: Any, error: BaseException, attempts: int) -> None:
        """Record a dead-lettered event in the store, best effort: it is in the dead-letter queue already,
        and failing here would only run the handlers and dead-letter it again."""
        if self._dead_letter_store is None or isinstance(error, PoisonMessageError):
            return
        cause = failure_cause(error)
        try:
            await self._dead_letter_store.add(
                EdaDeadLetterEntry(
                    event=self._serializer.deserialize(message.body),
                    error_type=type(cause).__name__,
                    error_message=str(cause),
                    attempts=attempts,
                )
            )
        except Exception:
            self.dead_letter_store_failures += 1
            logger.exception(
                "dead_letter_store_failed routing_key=%s message_id=%s: the event is in the dead-letter queue, not in "
                "the store",
                getattr(message, "routing_key", None),
                getattr(message, "message_id", None),
            )

    async def _stop_consumers(self) -> None:
        containers = list(self._containers)
        self._containers.clear()
        results = await asyncio.gather(*(container.stop() for container in containers), return_exceptions=True)
        for container, result in zip(containers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("eda_rabbitmq_listener_stop_failed container=%s: %s", container.name, result)

    async def stop(self) -> None:
        """Stop the consumers gracefully (the deliveries in flight finish, or are cancelled and requeued),
        then disconnect from RabbitMQ."""
        self._stopping = True
        try:
            await self._stop_consumers()
        finally:
            self._started = False
            self._stopping = False
            self._stopped = True
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            self._channel = self._exchange = None
