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
"""Granian ASGI server adapter — Rust/tokio-based, highest performance."""

from __future__ import annotations

import os
import time
from typing import Any

from pyfly.server.ports.server_stats import ServerStats
from pyfly.server.types import ServerInfo


class GranianServerAdapter:
    """ApplicationServerPort implementation backed by Granian.

    Granian uses Rust's Hyper + Tokio for network I/O, achieving
    ~3x the throughput of Uvicorn with significantly lower tail latency.

    Implements the optional :class:`~pyfly.server.ports.server_stats.ServerStatsPort`
    for workers + uptime only — Granian's Rust runtime exposes no Python-side
    connection/request handle, so those fields are always ``None`` (the pure-ASGI
    server-metrics middleware supplies them uniformly instead).
    """

    def __init__(self) -> None:
        self._server: Any = None
        self._info: ServerInfo | None = None
        self._serve_start_monotonic: float | None = None

    def serve(self, app: str | Any, config: Any) -> None:
        """Start Granian (blocking)."""
        import logging

        from granian import Granian
        from granian.constants import Interfaces

        # Suppress Granian's native [INFO] logs (Starting granian, Spawning worker-N)
        logging.getLogger("granian").setLevel(logging.WARNING)

        workers = config.workers if config.workers > 0 else 1
        event_loop = config.event_loop if config.event_loop != "auto" else "auto"
        http_mode = self._resolve_http_mode(config.http)

        granian_props = getattr(config, "granian", None)
        runtime_threads = getattr(granian_props, "runtime_threads", 1) if granian_props else 1
        runtime_mode = getattr(granian_props, "runtime_mode", "auto") if granian_props else "auto"
        backpressure = getattr(granian_props, "backpressure", None) if granian_props else None
        respawn = getattr(granian_props, "respawn_failed_workers", True) if granian_props else True

        host = getattr(config, "host", None) or "0.0.0.0"
        port = getattr(config, "port", None) or 8000

        kwargs: dict[str, Any] = {
            "target": app if isinstance(app, str) else app,
            "address": host,
            "port": port,
            "interface": Interfaces.ASGI,
            "http": http_mode,
            "workers": workers,
            "loop": event_loop,
            "log_level": "warning",
            "runtime_threads": runtime_threads,
            "runtime_mode": runtime_mode,
            "backlog": config.backlog,
            "respawn_failed_workers": respawn,
            "workers_kill_timeout": config.graceful_timeout,
        }
        if backpressure is not None:
            kwargs["backpressure"] = backpressure
        if config.ssl_certfile:
            kwargs["ssl_certfile"] = config.ssl_certfile
        if config.ssl_keyfile:
            kwargs["ssl_keyfile"] = config.ssl_keyfile

        server = Granian(**kwargs)
        self._server = server
        self._info = ServerInfo(
            name="granian",
            version=self._get_version(),
            workers=workers,
            event_loop=event_loop,
            http_protocol=config.http,
            host=host,
            port=port,
        )
        self.on_serve_start()
        server.serve()

    async def serve_async(self, app: str | Any, config: Any) -> None:
        """Start Granian (async wrapper)."""
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.serve, app, config)

    def shutdown(self) -> None:
        """Release server reference.

        Granian handles graceful shutdown internally via OS signals
        (SIGINT/SIGTERM).  When ``serve()`` is running, the Rust runtime
        intercepts the signal, stops accepting connections, waits for
        in-flight requests (up to ``workers_kill_timeout``), and then
        triggers the ASGI lifespan shutdown event which calls
        ``PyFlyApplication.shutdown()`` → ``ApplicationContext.stop()``.

        This method only releases the Python-side reference; it is not
        part of the normal shutdown path.
        """
        self._server = None

    # -- ServerStatsPort (best-effort) --------------------------------------

    def on_serve_start(self) -> None:
        """Record the server-bind moment (basis for ``server_uptime_seconds``)."""
        self._serve_start_monotonic = time.monotonic()

    def on_serve_stop(self) -> None:
        """No-op — Granian has no Python-side server handle to release."""

    def sample(self) -> ServerStats:
        """Workers + uptime only; connection/request fields are always ``None``."""
        workers = self._info.workers if self._info is not None else 1
        return ServerStats(workers=workers, server_uptime_seconds=self._uptime_seconds(), worker_pid=os.getpid())

    def _uptime_seconds(self) -> float:
        if self._serve_start_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._serve_start_monotonic)

    @property
    def server_info(self) -> ServerInfo:
        if self._info is not None:
            return self._info
        return ServerInfo(
            name="granian",
            version=self._get_version(),
            workers=0,
            event_loop="unknown",
            http_protocol="unknown",
            host="0.0.0.0",
            port=0,
        )

    @staticmethod
    def _get_version() -> str:
        try:
            from importlib.metadata import version

            return version("granian")
        except Exception:
            return "unknown"

    @staticmethod
    def _resolve_http_mode(http: str) -> Any:
        from granian.constants import HTTPModes

        mapping = {"auto": HTTPModes.auto, "1": HTTPModes.http1, "2": HTTPModes.http2}
        return mapping.get(http, HTTPModes.auto)
