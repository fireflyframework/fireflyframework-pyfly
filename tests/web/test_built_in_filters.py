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
"""Tests for built-in WebFilter implementations (transaction_id, logging, security_headers)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.container.ordering import HIGHEST_PRECEDENCE, get_order
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.adapters.starlette.filters import (
    RequestLoggingFilter,
    SecurityHeadersFilter,
    TransactionIdFilter,
)
from pyfly.web.security_headers import SecurityHeadersConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ok_handler(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


async def _tx_id_handler(request: Request) -> PlainTextResponse:
    """Echo back the transaction_id from request state."""
    tx_id = getattr(request.state, "transaction_id", "missing")
    return PlainTextResponse(tx_id)


async def _error_handler(request: Request) -> PlainTextResponse:
    raise ValueError("boom")


def _make_app(*filters, routes=None) -> Starlette:
    if routes is None:
        routes = [Route("/test", _ok_handler)]
    return Starlette(
        routes=routes,
        middleware=[Middleware(WebFilterChainMiddleware, filters=list(filters))],
    )


# ---------------------------------------------------------------------------
# TransactionIdFilter
# ---------------------------------------------------------------------------


class TestTransactionIdFilter:
    def test_generates_transaction_id(self):
        app = _make_app(TransactionIdFilter())
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert "X-Transaction-Id" in resp.headers
        # UUID format
        assert len(resp.headers["X-Transaction-Id"]) == 36

    def test_propagates_existing_transaction_id(self):
        app = _make_app(TransactionIdFilter())
        client = TestClient(app)
        resp = client.get("/test", headers={"X-Transaction-Id": "custom-123"})
        assert resp.headers["X-Transaction-Id"] == "custom-123"

    def test_sets_request_state(self):
        app = _make_app(
            TransactionIdFilter(),
            routes=[Route("/test", _tx_id_handler)],
        )
        client = TestClient(app)
        resp = client.get("/test", headers={"X-Transaction-Id": "my-id"})
        assert resp.text == "my-id"

    def test_order_is_highest_precedence_plus_100(self):
        assert get_order(TransactionIdFilter) == HIGHEST_PRECEDENCE + 100


# ---------------------------------------------------------------------------
# RequestLoggingFilter
# ---------------------------------------------------------------------------


class TestRequestLoggingFilter:
    def test_passes_through(self):
        app = _make_app(RequestLoggingFilter())
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.text == "OK"

    def test_propagates_exceptions(self):
        app = _make_app(
            RequestLoggingFilter(),
            routes=[Route("/test", _error_handler)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/test")
        assert resp.status_code == 500

    def test_order_is_highest_precedence_plus_200(self):
        assert get_order(RequestLoggingFilter) == HIGHEST_PRECEDENCE + 200


# ---------------------------------------------------------------------------
# SecurityHeadersFilter
# ---------------------------------------------------------------------------


class TestSecurityHeadersFilter:
    def test_adds_default_security_headers(self):
        app = _make_app(SecurityHeadersFilter())
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "Strict-Transport-Security" in resp.headers
        assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_custom_config(self):
        cfg = SecurityHeadersConfig(
            x_frame_options="SAMEORIGIN",
            content_security_policy="default-src 'self'",
        )
        app = _make_app(SecurityHeadersFilter(config=cfg))
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert resp.headers["Content-Security-Policy"] == "default-src 'self'"

    def test_order_is_highest_precedence_plus_300(self):
        assert get_order(SecurityHeadersFilter) == HIGHEST_PRECEDENCE + 300


# ---------------------------------------------------------------------------
# Ordering between built-in filters
# ---------------------------------------------------------------------------


class TestBuiltInFilterOrdering:
    def test_transaction_id_before_logging_before_security_headers(self):
        assert get_order(TransactionIdFilter) < get_order(RequestLoggingFilter)
        assert get_order(RequestLoggingFilter) < get_order(SecurityHeadersFilter)
