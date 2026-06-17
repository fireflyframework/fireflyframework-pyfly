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
"""Server lifecycle + identity meters, bound from the in-worker ASGI lifespan.

The :class:`ServerMetricsBinder` runs inside the wrapped ASGI lifespan (the one
adapter-agnostic place that executes in EVERY worker on the real event loop —
beside ``register_process_metrics`` and ``ManagementServer.start``). It emits the
server-layer meters that do not need a live server handle:

    ``server_workers``         (gauge)   — configured worker count (from
                                            ``_PYFLY_WORKERS``); per ``worker_pid``
    ``server_uptime_seconds``  (gauge)   — seconds since this worker bound, refreshed
                                            on a sampling tick
    ``server_started_total``   (counter) — incremented once per worker on startup
    ``server_stopped_total``   (counter) — incremented once per worker on graceful stop
    ``server_native_connections`` (gauge) — OPTIONAL: uvicorn's true socket count, set
                                            only when a :class:`ServerStatsPort` sample
                                            provides it (``None`` elsewhere)

The live ASGI-scope connection / in-flight / request gauges are owned by the
pure-ASGI :mod:`~pyfly.web.adapters.starlette.asgi_server_metrics` middleware
(single writer per metric).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

try:
    from prometheus_client import Counter, Gauge

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover - exercised only without the observability extra
    Counter = None  # type: ignore[assignment,misc]
    Gauge = None  # type: ignore[assignment,misc]
    _HAS_PROMETHEUS = False

from pyfly.server.ports.server_stats import ServerStatsPort

_LABELS = ["server", "worker_pid"]
_logger = logging.getLogger("pyfly.observability.server")

# Process-global collectors (one set per process, like the request timer).
_collectors: dict[str, Any] | None = None


def _get_binder_collectors() -> dict[str, Any]:
    """Get-or-create the process-global server lifecycle/identity meters."""
    global _collectors
    if _collectors is None:
        _collectors = {
            "workers": Gauge(
                "server_workers", "Configured server worker processes", _LABELS, multiprocess_mode="liveall"
            ),
            "uptime": Gauge(
                "server_uptime_seconds", "Seconds since this server worker bound", _LABELS, multiprocess_mode="liveall"
            ),
            "native_conns": Gauge(
                "server_native_connections",
                "Server-native socket connections (uvicorn; incl. idle keep-alive)",
                _LABELS,
                multiprocess_mode="livesum",
            ),
            "started": Counter("server_started", "Server worker startups", _LABELS),
            "stopped": Counter("server_stopped", "Server worker graceful stops", _LABELS),
        }
    return _collectors


def reset_collectors() -> None:
    """Unregister and drop the global collectors. Test-support only."""
    global _collectors
    if _collectors is None:
        return
    from prometheus_client import REGISTRY

    for collector in _collectors.values():
        with contextlib.suppress(KeyError, ValueError):
            REGISTRY.unregister(collector)
    _collectors = None


def resolve_worker_count(fallback: int = 1) -> int:
    """Worker count from the ``_PYFLY_WORKERS`` env (set by ``cli/run.py``)."""
    raw = os.environ.get("_PYFLY_WORKERS")
    if raw:
        with contextlib.suppress(ValueError):
            return max(1, int(raw))
    return max(1, fallback)


def build_binder_for_context(context: Any, *, sample_interval: float = 5.0) -> ServerMetricsBinder | None:
    """Build a :class:`ServerMetricsBinder` for a worker from its context.

    Server identity comes from the ``_PYFLY_SERVER_TYPE`` env var (set by
    ``cli/run.py`` and inherited by forked workers), falling back to the
    ``ApplicationServerPort`` bean's ``ServerInfo``. The same adapter bean
    supplies the optional :class:`ServerStatsPort` enrichment when it implements
    it. Shared by the Starlette and FastAPI ``create_app`` lifespans (DRY).
    """
    if context is None:
        return None
    server_name = os.environ.get("_PYFLY_SERVER_TYPE", "") or ""
    # "auto" is the unresolved server-type sentinel — fall back to the concrete
    # adapter name so the `server` metric label is uvicorn/granian/hypercorn.
    if server_name == "auto":
        server_name = ""
    stats_port: ServerStatsPort | None = None
    try:
        from pyfly.server.ports.outbound import ApplicationServerPort

        adapter = context.get_bean(ApplicationServerPort)
        if not server_name:
            server_name = adapter.server_info.name
        if isinstance(adapter, ServerStatsPort):
            stats_port = adapter
    except Exception as exc:  # noqa: BLE001 - binder is best-effort; never block startup
        _logger.debug("server_stats_adapter_unavailable", exc_info=exc)
        stats_port = None
    return ServerMetricsBinder(
        server_name=server_name or "unknown",
        workers=resolve_worker_count(),
        stats_port=stats_port,
        sample_interval=sample_interval,
    )


class ServerMetricsBinder:
    """Binds server lifecycle/identity meters for the lifetime of a worker."""

    def __init__(
        self,
        *,
        server_name: str,
        workers: int,
        stats_port: ServerStatsPort | None = None,
        sample_interval: float = 5.0,
    ) -> None:
        self._server_name = server_name
        self._workers = max(1, workers)
        self._stats_port = stats_port
        self._sample_interval = max(0.001, sample_interval)
        self._labels = (server_name, str(os.getpid()))
        self._start_monotonic: float | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Register meters, mark startup, and launch the refresh task."""
        if not _HAS_PROMETHEUS:
            return
        self._start_monotonic = time.monotonic()
        with contextlib.suppress(Exception):
            _get_binder_collectors()["started"].labels(*self._labels).inc()
        await self._refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the refresh task, set final uptime, mark graceful stop.

        Hardened so a sampling task that died mid-life (its stored exception is
        re-raised by ``await``) can NEVER prevent the final refresh, the
        ``server_stopped_total`` increment, or the multiprocess dead-worker
        cleanup from running — i.e. shutdown is always graceful.
        """
        if not _HAS_PROMETHEUS:
            return
        if self._task is not None:
            self._task.cancel()
            # suppress BOTH the normal CancelledError and any exception a
            # dead task stored, so cleanup below always proceeds.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self._refresh()
        with contextlib.suppress(Exception):
            _get_binder_collectors()["stopped"].labels(*self._labels).inc()
        # Multiprocess: drop this worker's live-gauge contributions so a recycled
        # worker's stale series do not linger in the aggregated scrape.
        with contextlib.suppress(Exception):
            from pyfly.observability.multiprocess import mark_worker_dead

            mark_worker_dead(os.getpid())

    def _uptime(self) -> float:
        if self._start_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._start_monotonic)

    async def _refresh(self) -> None:
        """Update the gauges; never raises.

        Every gauge write AND the (synchronous) ``sample()`` call are guarded:
        a transient failure (e.g. an mmap write error under multiprocess disk
        pressure, or a slow/raising third-party ``ServerStatsPort``) skips one
        tick instead of killing the sampler. ``sample()`` runs in a worker thread
        so a slow implementation cannot stall the event loop or delay drain.
        """
        try:
            cols = _get_binder_collectors()
            cols["workers"].labels(*self._labels).set(self._workers)
            cols["uptime"].labels(*self._labels).set(self._uptime())
            if self._stats_port is not None:
                sample = await asyncio.to_thread(self._stats_port.sample)
                if sample is not None and sample.active_connections is not None:
                    cols["native_conns"].labels(*self._labels).set(sample.active_connections)
        except Exception as exc:  # noqa: BLE001 - the binder must never crash the worker
            _logger.debug("server_metrics_refresh_failed", exc_info=exc)

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sample_interval)
                await self._refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a sampler must never die silently
            _logger.warning("server_metrics_sampler_stopped", exc_info=exc)
