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
"""Tests for the pure-ASGI server-metrics middleware."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY, generate_latest

from pyfly.web.adapters.starlette import asgi_server_metrics as asm
from pyfly.web.adapters.starlette.asgi_server_metrics import ServerMetricsASGIMiddleware


@pytest.fixture(autouse=True)
def _fresh_collectors():
    asm.reset_collectors()
    yield
    asm.reset_collectors()


def _labels() -> tuple[str, str]:
    import os

    return (asm._server_label(), str(os.getpid()))


async def _noop_app(scope, receive, send):  # noqa: ANN001
    pass


async def _drive_http(mw: ServerMetricsASGIMiddleware, path: str = "/api/x") -> None:
    await mw({"type": "http", "path": path}, lambda: None, lambda m: None)


class TestServerMetricsMiddleware:
    async def test_counts_completed_http_request(self) -> None:
        mw = ServerMetricsASGIMiddleware(_noop_app)
        await _drive_http(mw)
        # in-flight returns to zero; one request counted.
        assert mw._in_flight.labels(*_labels())._value.get() == 0.0
        assert mw._requests.labels(*_labels())._value.get() == 1.0
        exposition = generate_latest(REGISTRY).decode()
        assert "server_requests_total" in exposition
        assert "server_in_flight_requests" in exposition
        assert "server_active_connections" in exposition

    async def test_in_flight_decrements_even_on_error(self) -> None:
        async def boom(scope, receive, send):  # noqa: ANN001
            raise RuntimeError("kaboom")

        mw = ServerMetricsASGIMiddleware(boom)
        with pytest.raises(RuntimeError):
            await _drive_http(mw)
        assert mw._in_flight.labels(*_labels())._value.get() == 0.0
        assert mw._active.labels(*_labels())._value.get() == 0.0
        # a failed request still completed an http scope -> counted.
        assert mw._requests.labels(*_labels())._value.get() == 1.0

    async def test_websocket_counts_active_not_in_flight(self) -> None:
        mw = ServerMetricsASGIMiddleware(_noop_app)
        await mw({"type": "websocket", "path": "/ws"}, lambda: None, lambda m: None)
        # websocket touches active-connections but not the http in-flight/requests.
        assert mw._active.labels(*_labels())._value.get() == 0.0  # back to 0 after close
        assert mw._in_flight.labels(*_labels())._value.get() == 0.0
        assert mw._requests.labels(*_labels())._value.get() == 0.0

    async def test_lifespan_scope_passes_through_untouched(self) -> None:
        seen = {}

        async def app(scope, receive, send):  # noqa: ANN001
            seen["type"] = scope["type"]

        mw = ServerMetricsASGIMiddleware(app)
        await mw({"type": "lifespan"}, lambda: None, lambda m: None)
        assert seen["type"] == "lifespan"

    async def test_excluded_sse_path_not_counted(self) -> None:
        mw = ServerMetricsASGIMiddleware(_noop_app)
        await _drive_http(mw, path="/admin/api/sse/observability")
        assert mw._requests.labels(*_labels())._value.get() == 0.0

    async def test_disabled_middleware_is_passthrough(self) -> None:
        mw = ServerMetricsASGIMiddleware(_noop_app, enabled=False)
        await _drive_http(mw)
        # No collectors created/incremented when disabled.
        assert asm._requests is None or mw._requests.labels(*_labels())._value.get() == 0.0
