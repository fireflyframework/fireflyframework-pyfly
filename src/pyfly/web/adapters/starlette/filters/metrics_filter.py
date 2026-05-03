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
"""MetricsFilter — collects HTTP request metrics via prometheus_client."""

from __future__ import annotations

import time
from typing import Any

try:
    from prometheus_client import Counter, Gauge, Histogram
except ImportError:
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]

from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

# Module-level singletons — Prometheus collectors are registered globally,
# so they must only be created once per process.
_REQUESTS_TOTAL: Counter | None = None
_REQUEST_DURATION: Histogram | None = None
_ACTIVE_REQUESTS: Gauge | None = None

if Counter is not None:
    _REQUESTS_TOTAL = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["method", "path", "status"],
    )
    _REQUEST_DURATION = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["method", "path"],
    )
    _ACTIVE_REQUESTS = Gauge(
        "http_active_requests",
        "Number of in-flight HTTP requests",
    )


class MetricsFilter(OncePerRequestFilter):
    """Collects HTTP auto-instrumentation metrics.

    Metrics collected:
        - ``http_requests_total`` — counter by method, path, status
        - ``http_request_duration_seconds`` — histogram by method, path
        - ``http_active_requests`` — gauge of in-flight requests
    """

    __pyfly_order__ = -100  # Run early, after RequestContext

    exclude_patterns = ["/actuator/*", "/health", "/ready"]

    def __init__(self) -> None:
        assert _REQUESTS_TOTAL is not None, "prometheus_client is required for MetricsFilter"
        assert _REQUEST_DURATION is not None, "prometheus_client is required for MetricsFilter"
        assert _ACTIVE_REQUESTS is not None, "prometheus_client is required for MetricsFilter"
        self._requests_total: Counter = _REQUESTS_TOTAL
        self._request_duration: Histogram = _REQUEST_DURATION
        self._active_requests: Gauge = _ACTIVE_REQUESTS

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        method = request.method
        path = request.url.path

        self._active_requests.inc()
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.perf_counter() - start
            self._request_duration.labels(method=method, path=path).observe(duration)
            self._requests_total.labels(method=method, path=path, status=status).inc()
            self._active_requests.dec()
