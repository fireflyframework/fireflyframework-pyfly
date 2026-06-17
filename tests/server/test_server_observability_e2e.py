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
"""End-to-end: real uvicorn server + live server-observability metrics.

Boots the app on an ephemeral port with ``UvicornServerAdapter.serve_async`` (the
in-process embedding path), fires real HTTP requests through it, and asserts the
``server_*`` meters move and are served in the Prometheus exposition.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.observability import server_metrics as sm
from pyfly.server.adapters.uvicorn.adapter import UvicornServerAdapter
from pyfly.web.adapters.starlette import asgi_server_metrics as asm
from pyfly.web.adapters.starlette.app import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_collectors():
    asm.reset_collectors()
    sm.reset_collectors()
    yield
    asm.reset_collectors()
    sm.reset_collectors()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_ready(port: int, path: str, attempts: int = 50) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(attempts):
            with contextlib.suppress(Exception):
                r = await client.get(f"http://127.0.0.1:{port}{path}", timeout=1.0)
                if r.status_code == 200:
                    return
            await asyncio.sleep(0.05)
    raise AssertionError(f"server never became ready on :{port}{path}")


async def _hello(request: Any) -> JSONResponse:
    return JSONResponse({"ok": True})


async def test_server_metrics_move_under_real_traffic() -> None:
    port = _free_port()
    ctx = ApplicationContext(
        Config({"pyfly": {"server": {"observability": {"enabled": True, "sample-interval-seconds": 0.05}}}})
    )

    @contextlib.asynccontextmanager
    async def _lifespan(app: Any):
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = create_app(context=ctx, docs_enabled=False, extra_routes=[Route("/hello", _hello)], lifespan=_lifespan)

    from pyfly.config.properties.server import ServerProperties

    config = ServerProperties()
    config.host = "127.0.0.1"
    config.port = port

    adapter = UvicornServerAdapter()
    task = asyncio.create_task(adapter.serve_async(app, config))
    try:
        await _wait_ready(port, "/hello")
        async with httpx.AsyncClient() as client:
            for _ in range(5):
                r = await client.get(f"http://127.0.0.1:{port}/hello", timeout=2.0)
                assert r.status_code == 200

        # Give the binder's sampling task a tick to publish uptime/workers.
        await asyncio.sleep(0.1)

        exposition = generate_latest(REGISTRY).decode()
        assert "server_requests_total" in exposition
        assert "server_active_connections" in exposition
        assert "server_in_flight_requests" in exposition
        assert "server_workers" in exposition
        assert "server_uptime_seconds" in exposition

        # server_requests_total must reflect the real traffic we sent.
        total = sum(
            sample.value
            for metric in REGISTRY.collect()
            if metric.name == "server_requests"
            for sample in metric.samples
            if sample.name == "server_requests_total"
        )
        assert total >= 5.0

        # The /actuator/prometheus exposition path serves the server_* meters.
        from pyfly.actuator.endpoints.prometheus_endpoint import PrometheusEndpoint

        body = (await PrometheusEndpoint().handle())["body"]
        assert "server_requests_total" in body
        assert "server_workers" in body
    finally:
        adapter.shutdown()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8.0)
        if not task.done():
            task.cancel()
