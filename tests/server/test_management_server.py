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
"""Tests for the ManagementServer runner and the reuse-port socket helper."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.server.management_server import ManagementServer, make_reuse_socket


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_make_reuse_socket_sets_reuse_options() -> None:
    port = _free_port()
    sock = make_reuse_socket("127.0.0.1", port)
    try:
        # Some platforms (e.g. macOS) report a non-1 truthy value; assert "set".
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
        if hasattr(socket, "SO_REUSEPORT"):
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) != 0
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_start_serves_and_stop_closes() -> None:
    port = _free_port()

    async def _ping(request: Request) -> JSONResponse:
        return JSONResponse({"mgmt": True})

    app = Starlette(routes=[Route("/actuator/health", _ping)])
    server = ManagementServer(app, host="127.0.0.1", port=port)

    await server.start()
    resp = None
    try:
        for _ in range(80):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(f"http://127.0.0.1:{port}/actuator/health", timeout=1.0)
                break
            except Exception:
                await asyncio.sleep(0.1)
        assert resp is not None
        assert resp.status_code == 200
        assert resp.json() == {"mgmt": True}
    finally:
        await server.stop()

    # Port is free again after stop().
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))  # raises if still bound
