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
"""Separate management server (Spring ``management.server.*`` parity).

When ``pyfly.management.server.port`` names a port different from the application
port, pyfly runs a second in-process ASGI listener that serves only the actuator
and admin endpoints, keeping them off the public business port. The listener is a
lightweight embedded :class:`uvicorn.Server` task on the main app's event loop, so
it is adapter-agnostic (the main server may be Granian, Uvicorn or Hypercorn) and
works for both ``pyfly run`` and an external ``uvicorn main:app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import TYPE_CHECKING, Any, Literal

from pyfly.config.properties.management import ManagementServerProperties

if TYPE_CHECKING:
    from pyfly.core.config import Config

ManagementMode = Literal["shared", "separate", "disabled"]


def resolve_management_mode(config: Config, main_port: int) -> tuple[ManagementMode, ManagementServerProperties]:
    """Resolve how the management endpoints should be served.

    - ``shared``   : port unset or equal to *main_port* -> mount on the main app.
    - ``disabled`` : port == -1 -> no actuator/admin routes anywhere.
    - ``separate`` : a different positive port -> dedicated management listener.
    """
    props = ManagementServerProperties()
    # Defensive, mirrors admin/actuator binding (bad config -> keep defaults).
    with contextlib.suppress(Exception):
        props = config.bind(ManagementServerProperties)

    port = props.port
    if port is None:
        return "shared", props
    if port == -1:
        return "disabled", props
    if port == main_port:
        return "shared", props
    return "separate", props


def make_reuse_socket(host: str, port: int) -> socket.socket:
    """Create a TCP socket bound to (host, port) with address/port reuse.

    ``SO_REUSEPORT`` (where the platform supports it) lets every worker process
    bind the same management port; the kernel load-balances connections across
    them. Falls back to ``SO_REUSEADDR`` only (e.g. Windows), which is sufficient
    for the single-worker default.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            # pragma: no cover - platform without a working SO_REUSEPORT
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind((host, port))
        sock.listen()
        sock.set_inheritable(True)
    except BaseException:
        # Close the socket deterministically if binding/listening fails (e.g. the
        # management port is already in use) instead of leaking the fd.
        sock.close()
        raise
    return sock


class ManagementServer:
    """An embedded uvicorn server serving the management ASGI app.

    Runs as an asyncio task on the caller's event loop (the main app's lifespan),
    so it cooperates with the same loop and is adapter-agnostic. Shutdown is
    cooperative via ``should_exit``.
    """

    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Bind the management socket and start serving on the running loop."""
        import uvicorn

        class _NoSignalServer(uvicorn.Server):
            # The main server owns process signals; the embedded one must not
            # install its own handlers (they would clobber the main app's, and
            # fail off the main thread in multi-worker mode).
            def install_signal_handlers(self) -> None:
                return None

        self._sock = make_reuse_socket(self._host, self._port)
        config = uvicorn.Config(self._app, log_level="warning", lifespan="off")
        server = _NoSignalServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(sockets=[self._sock]))

    async def stop(self) -> None:
        """Signal the embedded server to exit, await it, and close the socket."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover - shutdown race
                self._task.cancel()
                # Drain the cancellation so the task is not left pending.
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
