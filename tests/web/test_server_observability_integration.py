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
"""Integration: server observability wired into create_app's lifespan."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from prometheus_client import REGISTRY, generate_latest
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.observability import server_metrics as sm
from pyfly.web.adapters.starlette import asgi_server_metrics as asm
from pyfly.web.adapters.starlette.app import create_app


@pytest.fixture(autouse=True)
def _reset_collectors():
    asm.reset_collectors()
    sm.reset_collectors()
    yield
    asm.reset_collectors()
    sm.reset_collectors()


async def _hello(request: Any) -> JSONResponse:
    return JSONResponse({"ok": True})


def _make_app(*, enabled: bool) -> tuple[Any, ApplicationContext]:
    ctx = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "server": {"observability": {"enabled": enabled, "sample-interval-seconds": 60}},
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
    return app, ctx


class TestServerObservabilityIntegration:
    def test_enabled_registers_and_exposes_server_metrics(self) -> None:
        app, _ctx = _make_app(enabled=True)
        with TestClient(app) as client:
            assert client.get("/hello").status_code == 200

        exposition = generate_latest(REGISTRY).decode()
        # Binder meters (worker/uptime/lifecycle).
        assert "server_workers" in exposition
        assert "server_uptime_seconds" in exposition
        assert "server_started_total" in exposition
        # ASGI middleware meters (connections/in-flight/requests).
        assert "server_active_connections" in exposition
        assert "server_in_flight_requests" in exposition
        assert "server_requests_total" in exposition

    def test_request_increments_server_requests_total(self) -> None:
        app, _ctx = _make_app(enabled=True)
        with TestClient(app) as client:
            client.get("/hello")
            client.get("/hello")
        # The pure-ASGI middleware counted both completed http scopes.
        total = sum(
            sample.value
            for metric in REGISTRY.collect()
            if metric.name == "server_requests"
            for sample in metric.samples
            if sample.name == "server_requests_total"
        )
        assert total >= 2.0

    def test_disabled_does_not_register_server_metrics(self) -> None:
        app, _ctx = _make_app(enabled=False)
        with TestClient(app) as client:
            client.get("/hello")

        exposition = generate_latest(REGISTRY).decode()
        assert "server_workers" not in exposition
        assert "server_active_connections" not in exposition
