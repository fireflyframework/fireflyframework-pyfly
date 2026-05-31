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
"""RabbitMQ message broker adapter — wraps aio-pika."""

from __future__ import annotations

from typing import Any

from pyfly.messaging.ports.outbound import MessageHandler
from pyfly.messaging.types import Message


class RabbitMQAdapter:
    """MessageBrokerPort implementation backed by RabbitMQ via aio-pika.

    Requires aio-pika to be installed (install the rabbitmq extra: pyfly[rabbitmq]).
    Uses a single direct exchange. Topics map to routing keys and queue names.
    """

    def __init__(
        self,
        url: str = "amqp://guest:guest@localhost/",
        exchange_name: str = "pyfly",
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._handlers: list[tuple[str, MessageHandler, str | None]] = []
        self._started = False

    async def publish(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        import aio_pika

        message = aio_pika.Message(body=value, headers=headers or {})  # type: ignore[arg-type]
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
        import aio_pika

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        self._exchange = await self._channel.declare_exchange(
            self._exchange_name, aio_pika.ExchangeType.DIRECT, durable=True
        )

        for topic, handler, group in self._handlers:
            await self._start_consumer(topic, handler, group)

        self._started = True

    async def _start_consumer(self, topic: str, handler: MessageHandler, group: str | None) -> None:
        import aio_pika

        queue_name = group or f"pyfly.{topic}"
        queue = await self._channel.declare_queue(queue_name, durable=True)
        await queue.bind(self._exchange, routing_key=topic)

        async def on_message(
            message: aio_pika.IncomingMessage,
            _handler: MessageHandler = handler,
            _topic: str = topic,
        ) -> None:
            async with message.process():
                msg = Message(
                    topic=_topic,
                    value=message.body,
                    headers={k: str(v) for k, v in (message.headers or {}).items()},
                )
                await _handler(msg)

        await queue.consume(on_message)

    async def stop(self) -> None:
        self._started = False
        if self._connection is not None:
            await self._connection.close()
