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
"""RabbitMQ message broker adapter — wraps aio-pika.

Each subscription is consumed by one
:class:`~pyfly.messaging.listener_container.RabbitListenerContainer` on a channel of its own, with a
bounded prefetch and manual acknowledgement: every delivery runs in a unit of work the container opens
and is acked only after that unit committed. A failed delivery is republished to its queue after a
back-off with its attempt count, then dead-lettered to ``<exchange>.dlx`` (queue ``<queue>.dlq``) after
its last attempt; the adapter's consumers share a concurrency limit sized from the datasource pool.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pyfly.kernel.lifecycle import CONSUMER_PHASE
from pyfly.messaging.listener_container import (
    ATTEMPT_HEADER,
    ConcurrencyLimit,
    ListenerContainerSettings,
    ListenerInvoker,
    RabbitDeadLetter,
    RabbitListenerContainer,
    listener_options,
)
from pyfly.messaging.ports.outbound import MessageHandler
from pyfly.messaging.types import Message

logger = logging.getLogger(__name__)


def queue_name_of(topic: str, group: str | None) -> str:
    """The queue a subscription consumes: its group, or ``pyfly.<topic>`` without one."""
    return group or f"pyfly.{topic}"


class RabbitMQAdapter:
    """MessageBrokerPort implementation backed by RabbitMQ via aio-pika.

    Requires aio-pika to be installed (install the rabbitmq extra: pyfly[rabbitmq]).
    Uses a single direct exchange. Topics map to routing keys and queue names.

    Parameters
    ----------
    url:
        AMQP connection URL.
    exchange_name:
        The durable direct exchange messages are published to.
    settings:
        The listener container settings (retry policy, unit of work, prefetch, concurrency, shutdown
        timeout); by default :class:`~pyfly.messaging.listener_container.ListenerContainerSettings`.
    dead_letter_exchange:
        The exchange a delivery goes to after its last attempt, with its queue name as the routing key,
        into the durable queue ``<queue>.dlq``. Defaults to ``<exchange_name>.dlx``.
    connection_factory:
        Opens the connection (``aio_pika.connect_robust`` by default), called with the URL.
    """

    #: A consumer: it stops before any ``@pre_destroy``, draining the deliveries in flight.
    phase = CONSUMER_PHASE
    #: The listener container retries and dead-letters deliveries (see ``wrap_listener``).
    manages_listener_errors = True

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        exchange_name: str = "pyfly",
        *,
        settings: ListenerContainerSettings | None = None,
        dead_letter_exchange: str | None = None,
        connection_factory: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._settings = settings or ListenerContainerSettings()
        self._dead_letter_exchange = dead_letter_exchange or f"{exchange_name}.dlx"
        self._connection_factory = connection_factory
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._handlers: list[tuple[str, MessageHandler, str | None]] = []
        self._containers: list[RabbitListenerContainer[Message]] = []
        invoker = ListenerInvoker(self._settings, name=f"rabbitmq {exchange_name}")
        settings_ = self._settings
        self._limit = ConcurrencyLimit(
            lambda: settings_.concurrency or invoker.suggested_concurrency(settings_.prefetch)
        )
        self._started = False

    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish a persistent message with a ``message_id`` a consumer can deduplicate on."""
        import aio_pika

        message = aio_pika.Message(
            body=value,
            headers=headers or {},  # type: ignore[arg-type]
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=uuid.uuid4().hex,
        )
        await self._exchange.publish(message, routing_key=topic)

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        group: str | None = None,
    ) -> None:
        self._handlers.append((topic, handler, group))
        # The ApplicationContext starts adapter beans before @message_listener
        # wiring subscribes, so a late subscription must bind its own queue now.
        if self._started:
            await self._start_consumer(topic, handler, group)

    async def start(self) -> None:
        if self._started:
            return
        import aio_pika

        connect = self._connection_factory or aio_pika.connect_robust
        self._connection = await connect(self._url)
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )
        self._started = True
        for topic, handler, group in self._handlers:
            await self._start_consumer(topic, handler, group)

    async def _start_consumer(self, topic: str, handler: MessageHandler, group: str | None) -> None:
        queue = queue_name_of(topic, group)
        options = listener_options(handler)
        routes = [RabbitDeadLetter(self._dead_letter_exchange, queue, f"{queue}.dlq")]
        if options is not None and options.dead_letter is not None:
            # The listener's own dead-letter topic first; if no queue is bound to it, the default route.
            routes.insert(0, RabbitDeadLetter(self._exchange_name, options.dead_letter))

        def convert(message: Any, attempt: int, _topic: str = topic) -> Message:
            return Message(
                topic=_topic,
                value=message.body,
                headers={k: str(v) for k, v in (message.headers or {}).items() if k != ATTEMPT_HEADER},
                message_id=message.message_id,
                delivery_attempt=attempt,
            )

        container: RabbitListenerContainer[Message] = RabbitListenerContainer(
            connection=self._connection,
            queue=queue,
            bindings=[(self._exchange_name, topic)],
            convert=convert,
            handler=handler,
            dead_letters=routes,
            settings=self._settings,
            limit=self._limit,
            retry=self._settings.retry.with_options(options),
            name=f"{topic}[{queue}]",
        )
        self._containers.append(container)
        try:
            await container.start()
        except BaseException:
            self._containers.remove(container)
            raise

    async def stop(self) -> None:
        """Stop every consumer gracefully (in parallel), then close the connection."""
        self._started = False
        containers = list(self._containers)
        self._containers.clear()
        results = await asyncio.gather(*(container.stop() for container in containers), return_exceptions=True)
        for container, result in zip(containers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("rabbitmq_listener_stop_failed container=%s: %s", container.name, result)
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
