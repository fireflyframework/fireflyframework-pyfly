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
"""Per-request footprint controls (v26.06.64): access-log opt-out + security headers."""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import RequestLoggingFilter


def _chain_filters(app: Any) -> list[Any]:
    mws = [m for m in app.user_middleware if m.cls is WebFilterChainMiddleware]
    return mws[0].kwargs["filters"] if mws else []


def test_request_logging_enabled_by_default() -> None:
    app = create_app(context=ApplicationContext(Config({})))
    assert any(isinstance(f, RequestLoggingFilter) for f in _chain_filters(app))


def test_request_logging_can_be_disabled_for_footprint() -> None:
    cfg = Config({"pyfly": {"web": {"request-logging": {"enabled": "false"}}}})
    app = create_app(context=ApplicationContext(cfg))
    assert not any(isinstance(f, RequestLoggingFilter) for f in _chain_filters(app))


def test_security_headers_still_applied_after_bulk_extend() -> None:
    async def hi(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True})

    client = TestClient(create_app(context=ApplicationContext(Config({})), extra_routes=[Route("/hi", hi)]))
    resp = client.get("/hi")
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "referrer-policy" in resp.headers
    assert "strict-transport-security" in resp.headers
