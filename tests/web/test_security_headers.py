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
"""Tests for SecurityHeadersMiddleware."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.security_headers import SecurityHeadersMiddleware
from pyfly.web.security_headers import SecurityHeadersConfig


async def _hello(request):  # noqa: ANN001
    return JSONResponse({"msg": "ok"})


def _make_client(config: SecurityHeadersConfig | None = None) -> TestClient:
    if config:
        middleware = [Middleware(SecurityHeadersMiddleware, config=config)]
    else:
        middleware = [Middleware(SecurityHeadersMiddleware)]
    app = Starlette(
        routes=[Route("/hello", _hello)],
        middleware=middleware,
    )
    return TestClient(app)


class TestSecurityHeadersMiddleware:
    def test_default_headers_applied(self) -> None:
        client = _make_client()
        resp = client.get("/hello")

        assert resp.status_code == 200
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert resp.headers["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains"
        assert resp.headers["X-XSS-Protection"] == "0"
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_custom_config(self) -> None:
        config = SecurityHeadersConfig(
            x_content_type_options="nosniff",
            x_frame_options="SAMEORIGIN",
            strict_transport_security="max-age=86400",
            x_xss_protection="1; mode=block",
            referrer_policy="no-referrer",
        )
        client = _make_client(config)
        resp = client.get("/hello")

        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert resp.headers["Strict-Transport-Security"] == "max-age=86400"
        assert resp.headers["X-XSS-Protection"] == "1; mode=block"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_csp_header_when_configured(self) -> None:
        config = SecurityHeadersConfig(content_security_policy="default-src 'self'")
        client = _make_client(config)
        resp = client.get("/hello")

        assert resp.headers["Content-Security-Policy"] == "default-src 'self'"

    def test_csp_header_absent_when_none(self) -> None:
        client = _make_client()
        resp = client.get("/hello")

        assert "Content-Security-Policy" not in resp.headers

    def test_permissions_policy_when_configured(self) -> None:
        config = SecurityHeadersConfig(permissions_policy="camera=()")
        client = _make_client(config)
        resp = client.get("/hello")

        assert resp.headers["Permissions-Policy"] == "camera=()"

    def test_config_frozen(self) -> None:
        config = SecurityHeadersConfig()
        with pytest.raises(FrozenInstanceError):
            config.x_frame_options = "SAMEORIGIN"  # type: ignore[misc]
