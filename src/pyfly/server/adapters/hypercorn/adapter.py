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
"""Hypercorn ASGI server adapter — HTTP/2 and HTTP/3 support."""

from __future__ import annotations

import asyncio
from typing import Any

from pyfly.server.types import ServerInfo


class HypercornServerAdapter:
    """ApplicationServerPort implementation backed by Hypercorn.

    The only mainstream Python ASGI server with HTTP/3 (QUIC) support.
    Also supports Trio as an alternative to asyncio.
    """

    def __init__(self) -> None:
        self._shutdown_event: asyncio.Event | None = None
        self._info: ServerInfo | None = None

    def serve(self, app: str | Any, config: Any) -> None:
        """Start Hypercorn (blocking)."""
        asyncio.run(self.serve_async(app, config))

    async def serve_async(self, app: str | Any, config: Any) -> None:
        """Start Hypercorn (async)."""
        from hypercorn.asyncio import serve  # type: ignore[import-not-found,unused-ignore]
        from hypercorn.config import Config as HypercornConfig  # type: ignore[import-not-found,unused-ignore]

        workers = config.workers if config.workers > 0 else 1
        host = getattr(config, "host", None) or "0.0.0.0"
        port = getattr(config, "port", None) or 8000

        hc_config = HypercornConfig()
        hc_config.bind = [f"{host}:{port}"]
        hc_config.workers = workers
        hc_config.loglevel = "WARNING"
        hc_config.keep_alive_timeout = config.keep_alive_timeout
        hc_config.backlog = config.backlog

        if config.ssl_certfile:
            hc_config.certfile = config.ssl_certfile
        if config.ssl_keyfile:
            hc_config.keyfile = config.ssl_keyfile

        event_loop = config.event_loop
        if event_loop == "uvloop":
            hc_config.worker_class = "uvloop"
        elif event_loop not in ("auto", "asyncio"):
            hc_config.worker_class = "asyncio"

        self._shutdown_event = asyncio.Event()
        self._info = ServerInfo(
            name="hypercorn",
            version=self._get_version(),
            workers=workers,
            event_loop=event_loop if event_loop != "auto" else "asyncio",
            http_protocol="h2" if config.ssl_certfile else "h1",
            host=host,
            port=port,
        )

        await serve(app, hc_config, shutdown_trigger=self._shutdown_event.wait)  # type: ignore[arg-type,unused-ignore]

    def shutdown(self) -> None:
        """Request graceful shutdown."""
        if self._shutdown_event is not None:
            self._shutdown_event.set()

    @property
    def server_info(self) -> ServerInfo:
        if self._info is not None:
            return self._info
        return ServerInfo(
            name="hypercorn",
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

            return version("hypercorn")
        except Exception:
            return "unknown"
