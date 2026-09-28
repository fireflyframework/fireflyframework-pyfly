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
"""Decorators for declarative message handling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def message_listener(
    topic: str,
    group: str | None = None,
    *,
    retries: int | None = None,
    retry_delay: float | None = None,
    dead_letter_topic: str | None = None,
) -> Callable[[F], F]:
    """Mark a method as a message listener for the given topic.

    On Kafka and RabbitMQ the listener container runs each delivery in a unit of work of its own (the
    method's ``@transactional`` joins it) and acknowledges it only after that unit committed. A failed
    delivery is attempted again after a back-off, then dead-lettered (see
    :mod:`pyfly.messaging.listener_container`); the arguments below override the container's
    ``pyfly.messaging.listener.retry.*`` settings for this listener.

    Args:
        topic: Topic to subscribe to.
        group: Optional consumer group.
        retries: Deliveries after the first one (``0``: dead-letter at the first failure). ``None``
            keeps the container's ``retry.max-attempts``.
        retry_delay: Linear back-off: attempt N+1 waits ``retry_delay * N`` seconds. ``None`` keeps the
            container's exponential back-off.
        dead_letter_topic: Where a message still failing after its last attempt goes (instead of
            ``<topic>.DLT`` on Kafka, ``<queue>.dlq`` on RabbitMQ), with ``x-original-topic`` /
            ``x-exception`` headers.
    """

    def decorator(func: F) -> F:
        func.__pyfly_message_listener__ = True  # type: ignore[attr-defined]
        func.__pyfly_listener_topic__ = topic  # type: ignore[attr-defined]
        func.__pyfly_listener_group__ = group  # type: ignore[attr-defined]
        func.__pyfly_listener_retries__ = retries  # type: ignore[attr-defined]
        func.__pyfly_listener_retry_delay__ = retry_delay  # type: ignore[attr-defined]
        func.__pyfly_listener_dlq__ = dead_letter_topic  # type: ignore[attr-defined]
        return func

    return decorator
