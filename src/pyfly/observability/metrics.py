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

from pyfly.observability.ports import MetricsRecorder

try:
    from prometheus_client import Counter, Gauge, Histogram

    _HAS_PROMETHEUS = True
except ImportError:
    _HAS_PROMETHEUS = False
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]

F = TypeVar("F", bound=Callable[..., Any])

# Metric collector caches are PROCESS-GLOBAL (module-level), not per-instance.
# prometheus_client registers every Counter/Histogram/Gauge on the process-global
# default ``REGISTRY`` keyed by metric name, so a name may only be created once per
# process. Caching per ``MetricsRegistry`` instance meant a second registry (e.g. a
# second ``create_app()`` in one pytest process) re-created an already-registered
# collector and prometheus raised "Duplicated timeseries in CollectorRegistry". Keying
# the caches at module scope makes get-or-create idempotent across every
# ``MetricsRegistry`` in the process, matching prometheus's one-collector-per-name model.
_COUNTERS: dict[str, Counter] = {}
_HISTOGRAMS: dict[str, Histogram] = {}
_GAUGES: dict[str, Gauge] = {}


class MetricsRegistry(MetricsRecorder):
    """Registry for application metrics — the Prometheus :class:`MetricsRecorder` adapter.

    Wraps prometheus_client to provide a clean API for creating and managing metrics.
    Registration is **process-global and idempotent**: collector caches live at module
    scope, so every metric name is created exactly once per process no matter how many
    ``MetricsRegistry`` instances exist. This mirrors prometheus_client's own
    one-collector-per-name model on the global default ``REGISTRY`` and means a second
    application (e.g. a second ``create_app()`` in one test process) reuses the existing
    collectors instead of raising "Duplicated timeseries in CollectorRegistry".
    """

    def __init__(self) -> None:
        if not _HAS_PROMETHEUS:
            raise ImportError(
                "prometheus_client is required for metrics. Install the observability extra: pyfly[observability]"
            )

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> Counter:
        """Get or create a counter metric (idempotent process-wide)."""
        if name not in _COUNTERS:
            _COUNTERS[name] = Counter(name, description, labels or [])
        return _COUNTERS[name]

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> Histogram:
        """Get or create a histogram metric (idempotent process-wide)."""
        if name not in _HISTOGRAMS:
            kwargs: dict[str, Any] = {}
            if buckets:
                kwargs["buckets"] = buckets
            _HISTOGRAMS[name] = Histogram(name, description, labels or [], **kwargs)
        return _HISTOGRAMS[name]

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Gauge:
        """Get or create a gauge metric (idempotent process-wide)."""
        if name not in _GAUGES:
            _GAUGES[name] = Gauge(name, description, labels or [])
        return _GAUGES[name]


def _sanitize(name: str) -> str:
    """Convert a Micrometer dot.case meter name to a Prometheus name."""
    return name.replace(".", "_").replace("-", "_")


def _class_method(func: Callable[..., Any]) -> tuple[str, str]:
    """Derive Micrometer ``class``/``method`` tags from a function's qualname."""
    qualname = getattr(func, "__qualname__", func.__name__)
    parts = qualname.split(".")
    method = func.__name__
    cls = parts[-2] if len(parts) >= 2 and parts[-2] != "<locals>" else ""
    return cls, method


def timed(
    registry: MetricsRegistry,
    name: str = "method.timed",
    description: str = "Timed method execution",
    *,
    extra_tags: dict[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorator that times a function, Micrometer ``@Timed`` style.

    The meter name accepts Micrometer dot.case (``orders.process``) and is exposed
    as a Prometheus timer ``<name>_seconds`` (``_count``/``_sum``/``_bucket``)
    tagged with ``class``, ``method``, ``exception`` (+ any ``extra_tags``).

    Usage:
        @timed(registry, "orders.process", "Order processing time")
        async def process(): ...
    """
    extra = extra_tags or {}
    prom_name = _sanitize(name)
    if not prom_name.endswith("_seconds"):
        prom_name += "_seconds"
    label_names = ["class", "method", "exception", *extra.keys()]

    def decorator(func: F) -> F:
        histogram = registry.histogram(prom_name, description, labels=label_names)
        cls, method = _class_method(func)

        def _labels(exception: str) -> dict[str, str]:
            return {"class": cls, "method": method, "exception": exception, **extra}

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                exception = "none"
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:
                    exception = type(exc).__name__
                    raise
                finally:
                    histogram.labels(**_labels(exception)).observe(time.perf_counter() - start)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            exception = "none"
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                exception = type(exc).__name__
                raise
            finally:
                histogram.labels(**_labels(exception)).observe(time.perf_counter() - start)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def counted(
    registry: MetricsRegistry,
    name: str = "method.counted",
    description: str = "Counted method invocations",
    *,
    extra_tags: dict[str, str] | None = None,
) -> Callable[[F], F]:
    """Decorator that counts invocations, Micrometer ``@Counted`` style.

    The meter name accepts Micrometer dot.case and is exposed as a Prometheus
    counter ``<name>_total`` tagged with ``class``, ``method``, ``result``
    (``success``/``failure``), ``exception`` (+ any ``extra_tags``).

    Usage:
        @counted(registry, "orders.created", "Orders created")
        async def create(): ...
    """
    extra = extra_tags or {}
    # prometheus_client appends ``_total`` itself; drop a user-supplied suffix.
    prom_name = _sanitize(name)
    if prom_name.endswith("_total"):
        prom_name = prom_name[: -len("_total")]
    label_names = ["class", "method", "result", "exception", *extra.keys()]

    def decorator(func: F) -> F:
        counter = registry.counter(prom_name, description, labels=label_names)
        cls, method = _class_method(func)

        def _labels(result: str, exception: str) -> dict[str, str]:
            return {"class": cls, "method": method, "result": result, "exception": exception, **extra}

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    result = await func(*args, **kwargs)
                except Exception as exc:
                    counter.labels(**_labels("failure", type(exc).__name__)).inc()
                    raise
                counter.labels(**_labels("success", "none")).inc()
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                counter.labels(**_labels("failure", type(exc).__name__)).inc()
                raise
            counter.labels(**_labels("success", "none")).inc()
            return result

        return sync_wrapper  # type: ignore[return-value]

    return decorator
