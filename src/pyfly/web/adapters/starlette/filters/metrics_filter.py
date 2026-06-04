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
"""MetricsFilter — Spring Boot / Micrometer-compatible HTTP server metrics.

Emits the same meter Spring Boot's ``WebMvcMetricsFilter`` produces, so existing
Grafana dashboards, Prometheus alerts, and tooling built for Spring Boot work
unchanged against a pyfly service:

    ``http_server_requests_seconds`` (Micrometer meter ``http.server.requests``)
        a request timer exposing ``_count`` and ``_sum`` (and ``_bucket`` when
        the histogram is enabled), tagged with the exact Micrometer tag set:
            * ``method``    — HTTP method (GET, POST, ...)
            * ``uri``       — the *templated* route (``/users/{id}``) — never the
                              raw path, which would explode label cardinality
            * ``status``    — numeric HTTP status ("200")
            * ``outcome``   — SUCCESS / CLIENT_ERROR / SERVER_ERROR / ...
            * ``exception`` — thrown exception class simple name, or "None"
    ``http_server_requests_seconds_max``
        a companion gauge holding the time-windowed maximum latency per tag set,
        mirroring Micrometer's separate ``..._max`` gauge.
"""

from __future__ import annotations

import time
from typing import Any

try:
    from prometheus_client import Gauge, Histogram, Summary
except ImportError:  # pragma: no cover - exercised only without the observability extra
    Gauge = None  # type: ignore[assignment,misc]
    Histogram = None  # type: ignore[assignment,misc]
    Summary = None  # type: ignore[assignment,misc]

from pyfly.web.filters import OncePerRequestFilter
from pyfly.web.ports.filter import CallNext

# Micrometer meter name -> Prometheus exposition name (base unit seconds).
_METRIC = "http_server_requests_seconds"
_MAX_METRIC = "http_server_requests_seconds_max"
_LABELS = ["method", "uri", "status", "outcome", "exception"]

# Micrometer's default service-level-objective histogram buckets (seconds). Only
# emitted when the histogram is explicitly enabled; the default is a Summary
# (``_count`` + ``_sum`` + ``_max`` gauge) exactly like Spring Boot's default.
_DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class _RollingMax:
    """Two-window rolling maximum, mirroring Micrometer's time-window max.

    Reports ``max(current_window, previous_window)`` so a single slow request
    decays out of the metric after at most ``2 * step`` seconds instead of
    pinning the gauge forever (which a monotonic max would do).
    """

    __slots__ = ("step", "_idx", "_cur", "_prev")

    def __init__(self, step: float = 60.0) -> None:
        self.step = step
        self._idx: int | None = None
        self._cur = 0.0
        self._prev = 0.0

    def record(self, value: float, now: float) -> float:
        idx = int(now / self.step)
        if self._idx is None:
            self._idx = idx
        elif idx != self._idx:
            # Carry the immediately-preceding window forward; older windows expire.
            self._prev = self._cur if idx == self._idx + 1 else 0.0
            self._cur = 0.0
            self._idx = idx
        if value > self._cur:
            self._cur = value
        return self._cur if self._cur >= self._prev else self._prev


# Process-global collectors. Prometheus collectors register against a global
# registry, so they must be created exactly once per process regardless of how
# many MetricsFilter instances exist.
_timer: Any = None
_max_gauge: Any = None
_max_by_key: dict[tuple[str, ...], _RollingMax] = {}


def _get_collectors(*, histogram: bool, buckets: tuple[float, ...] | None) -> tuple[Any, Any]:
    """Get-or-create the process-global request timer + max gauge."""
    global _timer, _max_gauge
    if _timer is None:
        if histogram:
            _timer = Histogram(
                _METRIC,
                "Duration of HTTP server request handling",
                _LABELS,
                buckets=buckets or _DEFAULT_BUCKETS,
            )
        else:
            _timer = Summary(_METRIC, "Duration of HTTP server request handling", _LABELS)
        _max_gauge = Gauge(_MAX_METRIC, "Max duration of HTTP server request handling", _LABELS)
    return _timer, _max_gauge


def reset_collectors() -> None:
    """Unregister and drop the global collectors. Test-support only."""
    global _timer, _max_gauge
    import contextlib

    from prometheus_client import REGISTRY

    for collector in (_timer, _max_gauge):
        if collector is not None:
            with contextlib.suppress(KeyError, ValueError):
                REGISTRY.unregister(collector)
    _timer = None
    _max_gauge = None
    _max_by_key.clear()


def _outcome(status_code: int) -> str:
    """Map an HTTP status to Micrometer's ``outcome`` tag value."""
    if 100 <= status_code < 200:
        return "INFORMATIONAL"
    if 200 <= status_code < 300:
        return "SUCCESS"
    if 300 <= status_code < 400:
        return "REDIRECTION"
    if 400 <= status_code < 500:
        return "CLIENT_ERROR"
    if 500 <= status_code < 600:
        return "SERVER_ERROR"
    return "UNKNOWN"


def _route_template(app: Any, endpoint: Any) -> str | None:
    """Recover the templated path (``/users/{id}``) for the matched endpoint.

    Starlette stores the matched ``endpoint`` in the ASGI scope but not the
    route template, so we resolve it back to the owning ``Route.path``.
    """
    routes = getattr(app, "routes", None) or []
    for route in routes:
        if getattr(route, "endpoint", None) is endpoint:
            return getattr(route, "path", None)
    return None


def _uri_tag(request: Any, status_code: int) -> str:
    """Compute the low-cardinality ``uri`` tag, matching Micrometer semantics."""
    scope = getattr(request, "scope", None) or {}
    endpoint = scope.get("endpoint")
    app = scope.get("app")
    if endpoint is not None and app is not None:
        template = _route_template(app, endpoint)
        if template:
            return template
    if status_code == 404:
        return "NOT_FOUND"
    if 300 <= status_code < 400:
        return "REDIRECTION"
    # A handler matched but we could not recover its template — fall back to the
    # raw path rather than dropping the observation entirely.
    return getattr(getattr(request, "url", None), "path", "UNKNOWN") or "UNKNOWN"


class MetricsFilter(OncePerRequestFilter):
    """Collects Spring Boot-compatible ``http.server.requests`` metrics.

    Runs early in the chain (after request-context setup) and times every
    request, tagging it the way Micrometer does so the Prometheus exposition is
    drop-in compatible with Spring Boot tooling.
    """

    __pyfly_order__ = -100  # Run early, just after RequestContext

    # Do not instrument the scrape endpoint itself (feedback noise) nor the
    # admin dashboard's long-lived SSE streams (they never "complete").
    exclude_patterns = ["/actuator/prometheus", "/admin/api/sse/*"]

    def __init__(self, *, histogram: bool = False, buckets: tuple[float, ...] | None = None) -> None:
        assert Summary is not None, "prometheus_client is required for MetricsFilter"
        self._timer, self._max_gauge = _get_collectors(histogram=histogram, buckets=buckets)

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        method = request.method
        start = time.perf_counter()
        exception = "None"
        status_code = 500
        try:
            response = await call_next(request)
            status_code = int(getattr(response, "status_code", 200))
            return response
        except Exception as exc:
            exception = type(exc).__name__
            status_code = 500
            raise
        finally:
            duration = time.perf_counter() - start
            uri = _uri_tag(request, status_code)
            outcome = _outcome(status_code)
            labels = (method, uri, str(status_code), outcome, exception)
            self._timer.labels(*labels).observe(duration)

            tracker = _max_by_key.get(labels)
            if tracker is None:
                tracker = _RollingMax()
                _max_by_key[labels] = tracker
            self._max_gauge.labels(*labels).set(tracker.record(duration, time.monotonic()))
