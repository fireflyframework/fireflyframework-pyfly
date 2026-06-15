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
"""End-to-end: actuator/admin on the management port, business on the main port.

Boots a real Uvicorn server on an ephemeral main port; the embedded management
server comes up on a second ephemeral port during the app lifespan.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.config.properties.server import ServerProperties
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter
from pyfly.web.adapters.starlette.app import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait(port: int, path: str, *, expect: int, timeout: float = 10.0) -> int:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Any = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
            if resp.status_code == expect:
                return resp.status_code
            last = resp.status_code
        except Exception as exc:  # noqa: BLE001 - connection refused while booting
            last = exc
        await asyncio.sleep(0.1)
    raise TimeoutError(f"http://127.0.0.1:{port}{path} never returned {expect} (last={last})")


@pytest.mark.asyncio
async def test_actuator_on_management_port_business_on_main_port() -> None:
    main_port = _free_port()
    mgmt_port = _free_port()

    async def _hello(request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "server": {"host": "127.0.0.1", "port": main_port},
                    "management": {
                        "server": {"port": mgmt_port, "address": "127.0.0.1"},
                        "endpoints": {"web": {"exposure": {"include": "*"}}},
                    },
                }
            }
        )
    )

    @contextlib.asynccontextmanager
    async def _lifespan(app: Any):
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = create_app(
        context=ctx,
        docs_enabled=False,
        extra_routes=[Route("/hello", _hello)],
        lifespan=_lifespan,
    )

    config = ServerProperties()
    config.host = "127.0.0.1"
    config.port = main_port
    adapter = UvicornServerAdapter()
    task = asyncio.create_task(adapter.serve_async(app, config))
    try:
        assert await _wait(main_port, "/hello", expect=200) == 200
        assert await _wait(mgmt_port, "/actuator/health", expect=200) == 200

        async with httpx.AsyncClient() as client:
            # actuator is NOT on the main (public) port
            r1 = await client.get(f"http://127.0.0.1:{main_port}/actuator/health", timeout=2.0)
            assert r1.status_code == 404
            # business path is NOT on the management port
            r2 = await client.get(f"http://127.0.0.1:{mgmt_port}/hello", timeout=2.0)
            assert r2.status_code == 404
    finally:
        adapter.shutdown()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8.0)
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
