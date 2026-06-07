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
"""Metrics recording port + a no-op adapter.

:class:`MetricsRecorder` is the abstraction application/framework instrumentation depends on,
so it is not hard-coupled to Prometheus. :class:`~pyfly.observability.metrics.MetricsRegistry`
is the default (Prometheus) adapter; :class:`NoOpMetricsRecorder` is a dependency-free adapter
for tests and for deployments that disable metrics — instrumentation code can always hold a
recorder instead of guarding ``None``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MetricsRecorder(Protocol):
    """Port for creating counter/histogram/gauge metrics (returns backend metric handles)."""

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> Any: ...

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> Any: ...

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Any: ...


class _NoOpMetric:
    """A metric handle that accepts every Prometheus-style operation and does nothing."""

    def labels(self, *args: Any, **kwargs: Any) -> _NoOpMetric:
        return self  # chainable, like prometheus_client's .labels(...)

    def inc(self, *args: Any, **kwargs: Any) -> None: ...

    def dec(self, *args: Any, **kwargs: Any) -> None: ...

    def set(self, *args: Any, **kwargs: Any) -> None: ...

    def observe(self, *args: Any, **kwargs: Any) -> None: ...

    def time(self, *args: Any, **kwargs: Any) -> _NoOpMetric:
        return self

    async def __aenter__(self) -> _NoOpMetric:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class NoOpMetricsRecorder:
    """Dependency-free :class:`MetricsRecorder` — every metric is a shared no-op handle."""

    def __init__(self) -> None:
        self._metric = _NoOpMetric()

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> Any:
        return self._metric

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> Any:
        return self._metric

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> Any:
        return self._metric
