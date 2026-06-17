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
"""Pure-ASGI server-metrics middleware — the uniform server-observability floor.

This wraps the application at the OUTERMOST layer (below Starlette's middleware
stack), so it sees the raw ASGI ``scope`` for every connection — including
websockets and requests the HTTP ``WebFilter`` chain never sees (it short-circuits
on ``scope["type"] != "http"``). It is the PRIMARY source for live server numbers
because it runs in-process in **every** worker, identically for uvicorn / granian
/ hypercorn and for any worker count (unlike a server's native stats, which on the
forked ``uvicorn.run(workers=N)`` path live in objects the in-worker lifespan
cannot reach).

Emits (single writer per metric; labels ``server`` + ``worker_pid``):

    ``server_active_connections`` (gauge)   — open http+websocket scopes
    ``server_in_flight_requests`` (gauge)   — http scopes currently handled
    ``server_requests_total``     (counter) — completed http scopes

``active_connections`` here is an ASGI-scope count, NOT a true socket count
(persistent keep-alive sockets the server holds idle are invisible to ASGI); the
uvicorn ``ServerStatsPort`` surfaces the true socket count separately.
"""

from __future__ import annotations

import os
from typing import Any

try:
    from prometheus_client import Counter, Gauge

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover - exercised only without the observability extra
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    _HAS_PROMETHEUS = False

_ACTIVE_METRIC = "server_active_connections"
_IN_FLIGHT_METRIC = "server_in_flight_requests"
# prometheus_client appends ``_total`` to a Counter name itself.
_REQUESTS_METRIC = "server_requests"
_LABELS = ["server", "worker_pid"]

# Long-lived SSE streams must not be counted — they would pin the gauges up for
# the lifetime of the stream (the admin Observability view itself is an SSE
# stream). Matched as a path SUBSTRING so a non-default admin path
# (pyfly.admin.path, e.g. /dashboard/api/sse/...) is excluded too.
_EXCLUDED_SUBSTRINGS = ("/api/sse/",)

# Process-global collectors (created once per process, like the request timer).
_active: Any = None
_in_flight: Any = None
_requests: Any = None


def _get_server_collectors() -> tuple[Any, Any, Any]:
    """Get-or-create the process-global server gauges + request counter.

    Gauges use ``multiprocess_mode="livesum"`` so that, under prometheus_client
    multiprocess mode, a scrape sums the live per-worker values (the value is
    harmless in single-process mode).
    """
    global _active, _in_flight, _requests
    if _active is None:
        _active = Gauge(
            _ACTIVE_METRIC, "Open ASGI connections (http + websocket)", _LABELS, multiprocess_mode="livesum"
        )
        _in_flight = Gauge(_IN_FLIGHT_METRIC, "In-flight HTTP requests", _LABELS, multiprocess_mode="livesum")
        _requests = Counter(_REQUESTS_METRIC, "Total HTTP requests handled at the server layer", _LABELS)
    return _active, _in_flight, _requests


def reset_collectors() -> None:
    """Unregister and drop the global collectors. Test-support only."""
    global _active, _in_flight, _requests
    import contextlib

    from prometheus_client import REGISTRY

    for collector in (_active, _in_flight, _requests):
        if collector is not None:
            with contextlib.suppress(KeyError, ValueError):
                REGISTRY.unregister(collector)
    _active = None
    _in_flight = None
    _requests = None


def _server_label() -> str:
    """The ``server`` label value — the configured server type, like the logs."""
    return os.environ.get("_PYFLY_SERVER_TYPE", "unknown")


class ServerMetricsASGIMiddleware:
    """Outermost pure-ASGI middleware counting connections + in-flight requests."""

    def __init__(self, app: Any, *, enabled: bool = True) -> None:
        self.app = app
        self._enabled = enabled and _HAS_PROMETHEUS
        if self._enabled:
            self._active, self._in_flight, self._requests = _get_server_collectors()
            self._label_values = (_server_label(), str(os.getpid()))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        typ = scope.get("type")
        if not self._enabled or typ not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(s in path for s in _EXCLUDED_SUBSTRINGS):
            await self.app(scope, receive, send)
            return

        active = self._active.labels(*self._label_values)
        active.inc()
        in_flight = None
        if typ == "http":
            in_flight = self._in_flight.labels(*self._label_values)
            in_flight.inc()
        try:
            await self.app(scope, receive, send)
        finally:
            active.dec()
            if in_flight is not None:
                in_flight.dec()
                self._requests.labels(*self._label_values).inc()
