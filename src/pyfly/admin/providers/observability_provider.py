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
"""Observability data provider — live server-layer metrics for the admin view.

Sources the ``server_*`` meters (and the ASGI server-metrics gauges) the
framework emits into the Prometheus registry, plus static ``ServerInfo``. Under
prometheus_client multiprocess mode it reads the aggregating registry so the
numbers reflect ALL workers, not just the one answering the admin request.

The snapshot shape (REST ``GET /admin/api/observability`` and the SSE
``observability`` event) is server-observability-specific: identity, aggregate
gauges, a per-worker breakdown (keyed by the ``worker_pid`` label), and a derived
requests/second.
"""

from __future__ import annotations

import os
import time
from typing import Any

# server_* sample names this view surfaces.
_ACTIVE = "server_active_connections"
_IN_FLIGHT = "server_in_flight_requests"
_REQUESTS = "server_requests_total"
_WORKERS = "server_workers"
_UPTIME = "server_uptime_seconds"
_STARTED = "server_started_total"
_STOPPED = "server_stopped_total"
_NATIVE_CONNS = "server_native_connections"


class ObservabilityProvider:
    """Provides live server-layer observability for the admin dashboard."""

    def __init__(self, context: Any = None) -> None:
        self._context = context

    # -- server identity (static ServerInfo, like ServerProvider) -----------

    def _resolve_server(self) -> Any:
        if self._context is None:
            return None
        try:
            from pyfly.server.ports.outbound import ApplicationServerPort
        except ImportError:
            return None
        for _cls, reg in self._context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, ApplicationServerPort):
                return reg.instance
        return None

    def _server_identity(self) -> dict[str, Any]:
        server = self._resolve_server()
        env_type = os.environ.get("_PYFLY_SERVER_TYPE")
        if server is None:
            return {
                "name": env_type or "unknown",
                "version": "unknown",
                "event_loop": os.environ.get("_PYFLY_EVENT_LOOP", "unknown"),
                "http": os.environ.get("_PYFLY_HTTP", "unknown"),
                "host": os.environ.get("_PYFLY_SERVER_HOST", "unknown"),
                "port": int(os.environ.get("_PYFLY_SERVER_PORT", "0") or 0),
            }
        info = server.server_info
        return {
            "name": info.name,
            "version": info.version,
            "event_loop": info.event_loop,
            "http": info.http_protocol,
            "host": info.host,
            "port": info.port,
        }

    # -- metric reading (multiprocess-aware) --------------------------------

    @staticmethod
    def _collect_server_samples() -> tuple[list[Any], bool]:
        """Return (samples, multiprocess) for ``server_*`` metric families."""
        try:
            import prometheus_client  # noqa: F401 - availability probe
        except ImportError:
            return [], False

        from pyfly.observability.multiprocess import collect_registry, is_multiprocess

        multiprocess = is_multiprocess()
        registry = collect_registry()
        samples: list[Any] = []
        for metric in registry.collect():
            if not metric.name.startswith("server"):
                continue
            for sample in metric.samples:
                if sample.name.endswith("_created"):
                    continue
                if sample.name.startswith("server_"):
                    samples.append(sample)
        return samples, multiprocess

    async def get_observability(self) -> dict[str, Any]:
        # Honor the feature flag: when server observability is disabled, report
        # unavailable so the dashboard renders its 'disabled' empty-state instead
        # of a populated-with-zeros view (the middleware/binder are not installed).
        if self._context is not None and not self._is_enabled():
            return {
                "timestamp": time.time(),
                "available": False,
                "has_prometheus": self._has_prometheus(),
                "multiprocess": False,
            }

        samples, multiprocess = self._collect_server_samples()
        has_prometheus = self._has_prometheus()

        # Aggregate totals (summed across worker_pid) + per-worker breakdown.
        totals: dict[str, float] = {}
        per_worker: dict[str, dict[str, Any]] = {}
        for sample in samples:
            totals[sample.name] = totals.get(sample.name, 0.0) + sample.value
            pid = sample.labels.get("worker_pid")
            if pid is not None:
                worker = per_worker.setdefault(pid, {"pid": pid})
                worker[sample.name] = worker.get(sample.name, 0.0) + sample.value

        active = totals.get(_ACTIVE)
        in_flight = totals.get(_IN_FLIGHT, 0.0)
        requests_total = totals.get(_REQUESTS)
        # server_workers is summed across pids; the live worker count is the
        # number of distinct reporting workers (each reports its own count).
        worker_count = len(per_worker) or int(totals.get(_WORKERS, 0.0))
        uptime = max((w.get(_UPTIME, 0.0) for w in per_worker.values()), default=totals.get(_UPTIME, 0.0))

        return {
            "timestamp": time.time(),
            "available": True,
            "has_prometheus": has_prometheus,
            "multiprocess": multiprocess,
            "server": self._server_identity(),
            "workers": worker_count,
            "uptime_seconds": uptime,
            "active_connections": int(active) if active is not None else None,
            "in_flight_requests": int(in_flight),
            "requests_total": int(requests_total) if requests_total is not None else None,
            # Snapshot default; the SSE stream computes the live per-consumer rate
            # from successive samples (so it is not corrupted by shared state).
            "requests_per_second": 0.0,
            "started_total": int(totals.get(_STARTED, 0.0)),
            "stopped_total": int(totals.get(_STOPPED, 0.0)),
            "per_worker": [self._worker_row(w) for w in per_worker.values()],
            "lifecycle": {
                "started_total": int(totals.get(_STARTED, 0.0)),
                "stopped_total": int(totals.get(_STOPPED, 0.0)),
            },
        }

    @staticmethod
    def _worker_row(worker: dict[str, Any]) -> dict[str, Any]:
        native = worker.get(_NATIVE_CONNS)
        return {
            "pid": worker.get("pid"),
            "uptime_seconds": worker.get(_UPTIME, 0.0),
            "in_flight_requests": int(worker.get(_IN_FLIGHT, 0.0)),
            "active_connections": int(worker.get(_ACTIVE, 0.0)),
            "requests_total": int(worker.get(_REQUESTS, 0.0)),
            # `is not None` (not truthiness) so a real 0 isn't shown as "n/a".
            "native_connections": int(native) if native is not None else None,
        }

    def _is_enabled(self) -> bool:
        try:
            return str(self._context.config.get("pyfly.server.observability.enabled", "true")).lower() in (
                "true",
                "1",
                "yes",
            )
        except Exception:  # noqa: BLE001 - default to enabled if config is unreadable
            return True

    @staticmethod
    def _has_prometheus() -> bool:
        try:
            import prometheus_client  # noqa: F401

            return True
        except ImportError:
            return False
