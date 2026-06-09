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

The adapter requires aio-pika to be installed (``pip install pyfly[rabbitmq]``
or ``pip install pyfly[eda]``).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.serializers import EventSerializer, JsonEventSerializer

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
    """

    def __init__(
        self,
        *,
        url: str = "amqp://guest:guest@localhost/",
        exchange_name: str = "pyfly",
        destinations: list[str] | None = None,
        group: str = "pyfly-default",
        serializer: EventSerializer | None = None,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._destinations = list(destinations) if destinations else ["pyfly.events"]
        self._group = group
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        self._handlers: list[tuple[str, EventHandler]] = []
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._started = False

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        """Register a handler for events matching *event_type_pattern*.

        If the bus has already been started, a consumer for every configured
        destination is bound immediately so the new handler starts receiving
        messages without a restart.
        """
        self._handlers.append((event_type_pattern, handler))

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish an event to *destination* on the exchange."""
        if not self._started:
            await self.start()
        import aio_pika

        from pyfly.eda.types import EventEnvelope

        envelope = EventEnvelope(
            event_type=event_type,
            payload=payload,
            destination=destination,
            headers=headers or {},
        )
        message = aio_pika.Message(
            body=self._serializer.serialize(envelope),
            headers=headers or {},  # type: ignore[arg-type]
        )
        await self._exchange.publish(message, routing_key=destination)

    async def start(self) -> None:
        """Connect to RabbitMQ and begin consuming from all destinations."""
        if self._started:
            return
        import aio_pika

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )

        for destination in self._destinations:
            await self._start_consumer(destination)

        self._started = True

    async def _start_consumer(self, destination: str) -> None:
        """Declare a queue for *destination*, bind it, and start consuming."""
        queue_name = f"{self._group}.{destination}"
        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._exchange, routing_key=destination)

        async def on_message(
            message: Any,
            _destination: str = destination,
        ) -> None:
            async with message.process():
                try:
                    envelope = self._serializer.deserialize(message.body)
                except Exception:
                    logger.exception(
                        "Failed to deserialize message from destination=%s",
                        _destination,
                    )
                    return
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

        await queue.consume(on_message)

    async def stop(self) -> None:
        """Disconnect from RabbitMQ."""
        self._started = False
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
