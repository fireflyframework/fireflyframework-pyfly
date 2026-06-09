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
"""Live-socket smoke tests for ASGI server adapters.

Each test binds an ephemeral localhost port (port 0), starts the server
adapter via ``serve_async()``, makes a real HTTP GET, then shuts down.
No Docker is required.

Adapter status:
- Uvicorn  : TESTED  — ``serve_async`` creates a ``uvicorn.Server`` that
  cooperates with asyncio cancellation via ``server.should_exit``.
- Hypercorn: TESTED  — ``serve_async`` exposes a ``shutdown_trigger`` event
  that integrates cleanly with the asyncio event loop.
- Granian  : SKIPPED — ``serve_async`` is implemented as
  ``loop.run_in_executor(None, self.serve, app, config)`` which offloads a
  blocking Rust/Granian server to a thread pool.  Granian registers OS signal
  handlers from that worker thread, which Python forbids
  (``ValueError: signal only works in main thread of the main interpreter``).
  There is no async-native cancellation path, so the test would either hang or
  kill the signal handler of the main thread.  Skip until Granian exposes a
  proper async API.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.config.properties.server import ServerProperties
from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 and return the assigned ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(port: int) -> Any:
    config = ServerProperties()
    config.host = "127.0.0.1"
    config.port = port
    return config


async def _wait_for_server(port: int, *, timeout: float = 5.0, interval: float = 0.1) -> None:
    """Poll until the server at *port* responds or *timeout* seconds elapse."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            async with httpx.AsyncClient() as client:
                await client.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                return  # server is up
        except Exception as exc:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Server on port {port} did not start within {timeout}s") from exc
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# ASGI app under test
# ---------------------------------------------------------------------------


async def _homepage(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


_SMOKE_APP = Starlette(routes=[Route("/", _homepage)])


# ---------------------------------------------------------------------------
# Uvicorn smoke test
# ---------------------------------------------------------------------------


class TestUvicornSmoke:
    """Live smoke test — starts a real Uvicorn instance on an ephemeral port."""

    @pytest.mark.asyncio
    async def test_serve_and_shutdown(self) -> None:
        port = _free_port()
        config = _make_config(port)
        adapter = UvicornServerAdapter()

        server_task = asyncio.create_task(adapter.serve_async(_SMOKE_APP, config))
        try:
            await _wait_for_server(port, timeout=5.0)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/", timeout=3.0)

            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            # Signal shutdown and wait for the task to complete.
            adapter.shutdown()
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await server_task


# ---------------------------------------------------------------------------
# Hypercorn smoke test
# ---------------------------------------------------------------------------


class TestHypercornSmoke:
    """Live smoke test — starts a real Hypercorn instance on an ephemeral port."""

    @pytest.mark.asyncio
    async def test_serve_and_shutdown(self) -> None:
        from importlib.util import find_spec

        if find_spec("hypercorn") is None:  # pragma: no cover
            pytest.skip("hypercorn not installed")

        from pyfly.server.adapters.hypercorn.adapter import HypercornServerAdapter

        port = _free_port()
        config = _make_config(port)
        adapter = HypercornServerAdapter()

        server_task = asyncio.create_task(adapter.serve_async(_SMOKE_APP, config))
        try:
            await _wait_for_server(port, timeout=8.0, interval=0.2)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/", timeout=3.0)

            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            adapter.shutdown()  # sets the shutdown event
            try:
                await asyncio.wait_for(server_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await server_task


# ---------------------------------------------------------------------------
# Granian smoke test — SKIPPED
# ---------------------------------------------------------------------------


class TestGranianSmoke:
    """Granian smoke test — skipped because serve_async is not safely cancellable.

    ``GranianServerAdapter.serve_async`` delegates to ``serve`` via
    ``asyncio.get_event_loop().run_in_executor(None, self.serve, app, config)``.
    The blocking Granian/Rust runtime tries to install OS signal handlers from
    the executor thread, which Python raises as:
    ``ValueError: signal only works in main thread of the main interpreter``.
    There is no cooperative async shutdown path; cancelling the task leaves the
    server running in the thread pool until the process exits.
    """

    @pytest.mark.asyncio
    async def test_serve_and_shutdown(self) -> None:
        pytest.skip(
            "GranianServerAdapter.serve_async runs in a thread pool via run_in_executor "
            "and registers OS signal handlers from that worker thread — "
            "Python forbids this (ValueError: signal only works in main thread). "
            "No cooperative async cancellation path exists; would hang the suite."
        )
