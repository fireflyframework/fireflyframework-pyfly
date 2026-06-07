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
    retries: int = 0,
    retry_delay: float = 0.0,
    dead_letter_topic: str | None = None,
) -> Callable[[F], F]:
    """Mark a method as a message listener for the given topic.

    Args:
        topic: Topic to subscribe to.
        group: Optional consumer group.
        retries: Times to re-invoke the handler on failure (linear ``retry_delay`` backoff).
        retry_delay: Base delay (seconds) between retries; attempt N waits ``retry_delay * N``.
        dead_letter_topic: When set, a message still failing after *retries* is re-published
            here (with ``x-original-topic`` / ``x-exception`` headers) instead of propagating.
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
