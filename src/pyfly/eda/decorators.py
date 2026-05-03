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
"""Decorators for declarative event publishing and consumption."""

from __future__ import annotations

import asyncio
import functools
import inspect
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.eda.ports.outbound import EventPublisher

F = TypeVar("F", bound=Callable[..., Any])


def event_publisher(
    bus: EventPublisher,
    destination: str,
    event_type: str,
    timing: str = "BEFORE",
) -> Callable[[F], F]:
    """Publish method arguments as events.

    Args:
        bus: Event bus instance.
        destination: Topic/queue name.
        event_type: Event type identifier.
        timing: When to publish — BEFORE, AFTER, or BOTH.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Build payload from args
            sig = inspect.signature(func)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            payload = _serialize_payload(dict(bound.arguments))

            if timing in ("BEFORE", "BOTH"):
                await bus.publish(destination, event_type, payload)

            result = await func(*args, **kwargs)

            if timing in ("AFTER", "BOTH"):
                await bus.publish(destination, event_type, payload)

            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def publish_result(
    bus: EventPublisher,
    destination: str,
    event_type: str,
    condition: Callable[..., bool] | None = None,
) -> Callable[[F], F]:
    """Publish method return value as an event.

    Args:
        bus: Event bus instance.
        destination: Topic/queue name.
        event_type: Event type identifier.
        condition: Optional predicate on the result — publish only if True.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await func(*args, **kwargs)

            should_publish = condition(result) if condition else True
            if should_publish:
                payload = result if isinstance(result, dict) else {"result": result}
                await bus.publish(destination, event_type, payload)

            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def event_listener(
    bus: EventPublisher,
    event_types: list[str],
) -> Callable[[F], F]:
    """Register a function as an event listener.

    Args:
        bus: Event bus instance.
        event_types: List of event type patterns to subscribe to.
    """

    def decorator(func: F) -> F:
        if not asyncio.iscoroutinefunction(func):
            warnings.warn(
                f"Event listener '{func.__name__}' is not async — it will be called in the event loop thread. "
                "Consider making it async for proper concurrency.",
                stacklevel=2,
            )
        for pattern in event_types:
            bus.subscribe(pattern, func)
        return func

    return decorator


def _serialize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert payload values to JSON-serializable types."""
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            result[key] = value
        elif hasattr(value, "__dict__"):
            result[key] = value.__dict__
        else:
            result[key] = value
    return result
