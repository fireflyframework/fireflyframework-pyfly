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
"""Uvicorn ASGI server adapter — ecosystem standard."""

from __future__ import annotations

import os
import time
from typing import Any

import uvicorn

from pyfly.server.ports.server_stats import ServerStats
from pyfly.server.types import ServerInfo

# Process-global reference to the live ``uvicorn.Server`` when this process runs
# the server in-process (the ``serve_async`` embedding path). It lets the
# ServerMetricsBinder read native ``server_state`` even when it holds a different
# adapter *instance* than the one serving. It is NOT set on the forked
# ``uvicorn.run(workers=N)`` production path — each worker builds its own Server
# inside uvicorn, so the binder falls back to the pure-ASGI middleware there.
_active_server: Any = None


class UvicornServerAdapter:
    """ApplicationServerPort implementation backed by Uvicorn.

    The most widely used Python ASGI server. Uses httptools + uvloop
    for optimal performance when ``uvicorn[standard]`` is installed.

    Also implements the optional :class:`~pyfly.server.ports.server_stats.ServerStatsPort`
    (best-effort): on the ``serve_async`` path it surfaces uvicorn's true socket
    connection count and total requests from ``Server.server_state``.
    """

    def __init__(self) -> None:
        self._server: Any = None
        self._info: ServerInfo | None = None
        self._serve_start_monotonic: float | None = None

    @staticmethod
    def _build_kwargs(host: str, port: int, loop: str, config: Any) -> dict[str, Any]:
        """Build the uvicorn keyword args common to serve() and serve_async().

        Shared so the async path honors the same SSL / keep-alive / backlog /
        graceful-shutdown / concurrency settings as the blocking path (#226).
        """
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "loop": loop,
            "http": "auto",
            "log_level": "warning",
            "timeout_keep_alive": config.keep_alive_timeout,
            "backlog": config.backlog,
        }
        if config.graceful_timeout:
            kwargs["timeout_graceful_shutdown"] = config.graceful_timeout
        if config.ssl_certfile:
            kwargs["ssl_certfile"] = config.ssl_certfile
        if config.ssl_keyfile:
            kwargs["ssl_keyfile"] = config.ssl_keyfile
        if config.max_concurrent_connections:
            kwargs["limit_concurrency"] = config.max_concurrent_connections
        if config.max_requests_per_worker:
            kwargs["limit_max_requests"] = config.max_requests_per_worker
        return kwargs

    def serve(self, app: str | Any, config: Any) -> None:
        """Start Uvicorn (blocking)."""
        workers = config.workers if config.workers > 0 else 1
        host = getattr(config, "host", None) or "0.0.0.0"
        port = getattr(config, "port", None) or 8000
        loop = config.event_loop if config.event_loop != "auto" else "auto"

        kwargs = self._build_kwargs(host, port, loop, config)
        kwargs["workers"] = workers

        self._info = ServerInfo(
            name="uvicorn",
            version=self._get_version(),
            workers=workers,
            event_loop=loop,
            http_protocol="h1",
            host=host,
            port=port,
        )

        uvicorn.run(app, **kwargs)

    async def serve_async(self, app: str | Any, config: Any) -> None:
        """Start Uvicorn (async)."""
        workers = config.workers if config.workers > 0 else 1
        host = getattr(config, "host", None) or "0.0.0.0"
        port = getattr(config, "port", None) or 8000
        loop = config.event_loop if config.event_loop != "auto" else "auto"

        uvi_config = uvicorn.Config(app, **self._build_kwargs(host, port, loop, config))
        server = uvicorn.Server(uvi_config)
        self._server = server
        self._info = ServerInfo(
            name="uvicorn",
            version=self._get_version(),
            workers=workers,
            event_loop=config.event_loop,
            http_protocol="h1",
            host=host,
            port=port,
        )
        global _active_server
        _active_server = server
        self.on_serve_start()
        try:
            await server.serve()
        finally:
            self.on_serve_stop()

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True

    # -- ServerStatsPort (best-effort) --------------------------------------

    def on_serve_start(self) -> None:
        """Record the server-bind moment (basis for ``server_uptime_seconds``)."""
        self._serve_start_monotonic = time.monotonic()

    def on_serve_stop(self) -> None:
        """Clear the process-global live-server reference."""
        global _active_server
        if _active_server is self._server:
            _active_server = None

    def sample(self) -> ServerStats | None:
        """Sample live uvicorn stats when a server runs in this process.

        Reads ``Server.server_state`` (total requests + the live connection set)
        from this adapter's own server or, failing that, the process-global
        ``_active_server``. Returns ``None`` connection/request fields when no
        in-process server handle is available (the forked production path).
        """
        srv = self._server or _active_server
        active_connections: int | None = None
        total_requests: int | None = None
        state = getattr(srv, "server_state", None)
        if state is not None:
            conns = getattr(state, "connections", None)
            if conns is not None:
                active_connections = len(conns)
            total_requests = getattr(state, "total_requests", None)
        workers = self._info.workers if self._info is not None else 1
        return ServerStats(
            workers=workers,
            server_uptime_seconds=self._uptime_seconds(),
            worker_pid=os.getpid(),
            active_connections=active_connections,
            total_requests=total_requests,
        )

    def _uptime_seconds(self) -> float:
        if self._serve_start_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._serve_start_monotonic)

    @property
    def server_info(self) -> ServerInfo:
        if self._info is not None:
            return self._info
        return ServerInfo(
            name="uvicorn",
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

            return version("uvicorn")
        except Exception:
            return "unknown"
