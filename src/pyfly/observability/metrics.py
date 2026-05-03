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
"""Metrics collection with Prometheus-compatible counters and histograms."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any, TypeVar

try:
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]

F = TypeVar("F", bound=Callable[..., Any])


class MetricsRegistry:
    """Registry for application metrics.

    Wraps prometheus_client to provide a clean API for creating and
    managing metrics. Ensures each metric name is registered only once.
    """

    def __init__(self) -> None:
        if not _HAS_PROMETHEUS:
            raise ImportError(
                "prometheus_client is required for metrics. Install the observability extra: pyfly[observability]"
            )
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> Counter:
        """Get or create a counter metric."""
        if name not in self._counters:
            self._counters[name] = Counter(name, description, labels or [])
        return self._counters[name]

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> Histogram:
        """Get or create a histogram metric."""
        if name not in self._histograms:
            kwargs: dict[str, Any] = {}
            if buckets:
                kwargs["buckets"] = buckets
            self._histograms[name] = Histogram(name, description, labels or [], **kwargs)
        return self._histograms[name]

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Gauge:
        """Get or create a gauge metric."""
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, description, labels or [])
        return self._gauges[name]


def timed(registry: MetricsRegistry, name: str, description: str) -> Callable[[F], F]:
    """Decorator that records function execution duration as a histogram.

    Usage:
        @timed(registry, "request_duration_seconds", "Request processing time")
        async def handle_request(): ...
    """

    def decorator(func: F) -> F:
        histogram = registry.histogram(name, description)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                try:
                    return await func(*args, **kwargs)
                finally:
                    histogram.observe(time.perf_counter() - start)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                histogram.observe(time.perf_counter() - start)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def counted(registry: MetricsRegistry, name: str, description: str) -> Callable[[F], F]:
    """Decorator that counts function invocations.

    Usage:
        @counted(registry, "requests_total", "Total request count")
        async def handle_request(): ...
    """

    def decorator(func: F) -> F:
        counter = registry.counter(name, description)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                counter.inc()
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            counter.inc()
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator
