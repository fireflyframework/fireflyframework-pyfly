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

from typing import Any

import uvicorn

from pyfly.server.types import ServerInfo


class UvicornServerAdapter:
    """ApplicationServerPort implementation backed by Uvicorn.

    The most widely used Python ASGI server. Uses httptools + uvloop
    for optimal performance when ``uvicorn[standard]`` is installed.
    """

    def __init__(self) -> None:
        self._server: Any = None
        self._info: ServerInfo | None = None

    def serve(self, app: str | Any, config: Any) -> None:
        """Start Uvicorn (blocking)."""
        workers = config.workers if config.workers > 0 else 1
        host = getattr(config, "host", None) or "0.0.0.0"
        port = getattr(config, "port", None) or 8000
        loop = config.event_loop if config.event_loop != "auto" else "auto"

        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "workers": workers,
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

        uvi_config = uvicorn.Config(
            app,
            host=host,
            port=port,
            loop=config.event_loop if config.event_loop != "auto" else "auto",
            log_level="warning",
        )
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
        await server.serve()

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        if self._server is not None:
            self._server.should_exit = True

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
