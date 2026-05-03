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
"""Tests for CORS configuration and middleware integration."""

from __future__ import annotations

import dataclasses

import pytest
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.cors import CORSConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def hello(request):
    return JSONResponse({"msg": "hello"})


HELLO_ROUTE = Route("/hello", hello)


# ---------------------------------------------------------------------------
# CORSConfig dataclass tests
# ---------------------------------------------------------------------------


class TestCORSConfigDefaults:
    """Default CORSConfig has sensible values."""

    def test_cors_config_defaults(self):
        cfg = CORSConfig()

        assert cfg.allowed_origins == ["*"]
        assert cfg.allowed_methods == ["GET"]
        assert cfg.allowed_headers == ["*"]
        assert cfg.allow_credentials is False
        assert cfg.exposed_headers == []
        assert cfg.max_age == 600


class TestCORSConfigCustom:
    """Custom values override defaults."""

    def test_cors_config_custom(self):
        cfg = CORSConfig(
            allowed_origins=["http://example.com"],
            allowed_methods=["GET", "POST", "PUT"],
            allowed_headers=["Authorization", "Content-Type"],
            allow_credentials=True,
            exposed_headers=["X-Custom-Header"],
            max_age=3600,
        )

        assert cfg.allowed_origins == ["http://example.com"]
        assert cfg.allowed_methods == ["GET", "POST", "PUT"]
        assert cfg.allowed_headers == ["Authorization", "Content-Type"]
        assert cfg.allow_credentials is True
        assert cfg.exposed_headers == ["X-Custom-Header"]
        assert cfg.max_age == 3600


class TestCORSConfigFrozen:
    """Cannot modify after creation."""

    def test_cors_config_frozen(self):
        cfg = CORSConfig()

        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.allow_credentials = True  # type: ignore[misc]

        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.max_age = 9999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration tests — CORS middleware via create_app()
# ---------------------------------------------------------------------------


class TestCORSPreflightRequest:
    """OPTIONS request with Origin header gets CORS headers."""

    def setup_method(self):
        app = create_app(
            title="test",
            cors=CORSConfig(
                allowed_origins=["http://example.com"],
                allowed_methods=["GET", "POST"],
            ),
            extra_routes=[HELLO_ROUTE],
        )
        self.client = TestClient(app)

    def test_cors_preflight_request(self):
        resp = self.client.options(
            "/hello",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )

        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "http://example.com"
        assert "POST" in resp.headers["access-control-allow-methods"]


class TestCORSSimpleRequest:
    """GET with Origin header gets Access-Control-Allow-Origin."""

    def setup_method(self):
        app = create_app(
            title="test",
            cors=CORSConfig(allowed_origins=["http://example.com"]),
            extra_routes=[HELLO_ROUTE],
        )
        self.client = TestClient(app)

    def test_cors_simple_request(self):
        resp = self.client.get(
            "/hello",
            headers={"Origin": "http://example.com"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"msg": "hello"}
        assert resp.headers["access-control-allow-origin"] == "http://example.com"


class TestNoCORSWhenNotConfigured:
    """create_app() without cors param has no CORS headers."""

    def setup_method(self):
        app = create_app(
            title="test",
            extra_routes=[HELLO_ROUTE],
        )
        self.client = TestClient(app)

    def test_no_cors_when_not_configured(self):
        resp = self.client.get(
            "/hello",
            headers={"Origin": "http://example.com"},
        )

        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers
