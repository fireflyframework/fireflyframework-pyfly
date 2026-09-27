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
"""Retry + dead-letter handling for ``@message_listener`` handlers.

On a broker that consumes through a listener container (Kafka, RabbitMQ: it declares
``manages_listener_errors = True``), the container retries and dead-letters each delivery itself, outside
the delivery's unit of work, with its attempts acknowledged to the broker only once they committed (see
:mod:`pyfly.messaging.listener_container`). :func:`wrap_listener` then only attaches the listener's own
``retries`` / ``retry_delay`` / ``dead_letter_topic`` to the handler, as
:class:`~pyfly.messaging.listener_container.ListenerOptions`.

On any other broker (the in-memory one), the handler is retried in process and dead-lettered by
republishing, as it always was.
"""

from __future__ import annotations

import asyncio
import functools
import logging

from pyfly.messaging.listener_container import (
    LinearBackOff,
    ListenerEndpoint,
    ListenerOptions,
    manages_listener_errors,
)
from pyfly.messaging.ports.outbound import MessageBrokerPort, MessageHandler
from pyfly.messaging.types import Message

logger = logging.getLogger(__name__)


def wrap_listener(
    handler: MessageHandler,
    broker: MessageBrokerPort,
    *,
    retries: int | None = 0,
    retry_delay: float | None = 0.0,
    dead_letter_topic: str | None = None,
) -> MessageHandler:
    """Wrap *handler* for *broker* with the listener's retry and dead-letter settings.

    - A broker with a listener container gets a :class:`ListenerEndpoint` carrying the settings; ``None``
      keeps the container's own (``retries`` is the number of deliveries after the first one, and
      ``retry_delay`` a linear back-off: attempt N+1 waits ``retry_delay * N`` seconds).
    - Any other broker gets the in-process wrapper: a failing message is retried up to *retries* times
      (linear ``retry_delay`` back-off) and, if still failing and *dead_letter_topic* is set, re-published
      there with diagnostic headers. With no retries and no DLQ, *handler* is returned unchanged (zero
      overhead).
    """
    if manages_listener_errors(broker):
        return ListenerEndpoint(
            handler,
            ListenerOptions(
                max_attempts=None if retries is None else max(0, retries) + 1,
                backoff=None if retry_delay is None else LinearBackOff(retry_delay),
                dead_letter=dead_letter_topic,
            ),
        )
    retries = retries or 0
    retry_delay = retry_delay or 0.0
    if retries <= 0 and dead_letter_topic is None:
        return handler

    @functools.wraps(handler)
    async def wrapped(message: Message) -> None:
        attempt = 0
        while True:
            try:
                await handler(message)
                return
            except Exception as exc:  # noqa: BLE001 - the listener contract is to handle/route, not crash the consumer
                if attempt < retries:
                    attempt += 1
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay * attempt)
                    logger.warning(
                        "message_listener retry %d/%d for topic %s: %s", attempt, retries, message.topic, exc
                    )
                    continue
                if dead_letter_topic is not None:
                    await broker.publish(
                        dead_letter_topic,
                        message.value,
                        key=message.key,
                        headers={
                            **message.headers,
                            "x-original-topic": message.topic,
                            "x-exception": type(exc).__name__,
                        },
                    )
                    logger.error(
                        "message_listener exhausted %d retries; routed topic %s -> DLQ %s",
                        retries,
                        message.topic,
                        dead_letter_topic,
                    )
                    return
                raise

    return wrapped
