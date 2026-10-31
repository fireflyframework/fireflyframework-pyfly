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
"""Distributed tracing with OpenTelemetry span decorator."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any, TypeVar

try:
    from opentelemetry import trace

    _HAS_OTEL = True
    _tracer = trace.get_tracer("pyfly")
except ImportError:
    _HAS_OTEL = False
    trace = None  # type: ignore[assignment]
    _tracer = None  # type: ignore[assignment]

F = TypeVar("F", bound=Callable[..., Any])


def span(name: str, *, include_content: bool = False) -> Callable[[F], F]:
    """Decorator that wraps a function in an OpenTelemetry span.

    No-op when opentelemetry is not installed. Exception content is withheld
    unless the caller explicitly opts in with ``include_content=True``.

    Usage:
        @span("process-order")
        async def process_order(order_id: str) -> dict: ...
    """
    if type(include_content) is not bool:
        raise ValueError("include_content must be a boolean")
    if not _HAS_OTEL:

        def noop_decorator(func: F) -> F:
            return func

        return noop_decorator

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with _tracer.start_as_current_span(
                    name, record_exception=False, set_status_on_exception=False
                ) as current_span:
                    try:
                        return await func(*args, **kwargs)
                    except BaseException as exc:
                        _record_error(current_span, exc, include_content=include_content)
                        raise

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            with _tracer.start_as_current_span(
                name, record_exception=False, set_status_on_exception=False
            ) as current_span:
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:
                    _record_error(current_span, exc, include_content=include_content)
                    raise

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def _record_error(current_span: Any, error: BaseException, *, include_content: bool) -> None:
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(error) if include_content else None))
    if include_content:
        current_span.record_exception(error)
    else:
        error_type = type(error)
        name = error_type.__qualname__
        if error_type.__module__ != "builtins":
            name = f"{error_type.__module__}.{name}"
        current_span.add_event("exception", attributes={"exception.type": name})
