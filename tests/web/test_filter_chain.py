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
"""Tests for WebFilterChainMiddleware — ordering, short-circuit, conditional skip."""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.container.ordering import HIGHEST_PRECEDENCE, get_order, order
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.filters import OncePerRequestFilter

# ---------------------------------------------------------------------------
# Test filters
# ---------------------------------------------------------------------------


@order(HIGHEST_PRECEDENCE + 10)
class HeaderFilter(OncePerRequestFilter):
    """Adds X-Filter-A header to every response."""

    async def do_filter(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Filter-A"] = "applied"
        return response


@order(HIGHEST_PRECEDENCE + 20)
class TraceFilter(OncePerRequestFilter):
    """Adds X-Filter-B header — runs after HeaderFilter by order."""

    async def do_filter(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Filter-B"] = "applied"
        return response


@order(5)
class ApiOnlyFilter(OncePerRequestFilter):
    """Only applies to /api/* paths."""

    url_patterns = ["/api/*"]

    async def do_filter(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Api-Filter"] = "applied"
        return response


@order(10)
class ShortCircuitFilter(OncePerRequestFilter):
    """Returns 429 without calling next — simulates rate limiting."""

    async def do_filter(self, request, call_next):
        return JSONResponse({"error": "rate limited"}, status_code=429)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ok_handler(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


def _make_app(*filters) -> Starlette:
    return Starlette(
        routes=[
            Route("/test", _ok_handler),
            Route("/api/data", _ok_handler),
            Route("/health", _ok_handler),
        ],
        middleware=[Middleware(WebFilterChainMiddleware, filters=list(filters))],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFilterChainOrdering:
    def test_filters_applied_in_order(self):
        """Both filters should run; headers from both should appear."""
        app = _make_app(HeaderFilter(), TraceFilter())
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.headers["X-Filter-A"] == "applied"
        assert resp.headers["X-Filter-B"] == "applied"

    def test_order_decorator_determines_sequence(self):
        """Verify get_order() works correctly on our test filters."""
        assert get_order(HeaderFilter) < get_order(TraceFilter)
        assert get_order(TraceFilter) < get_order(ApiOnlyFilter)


class TestFilterChainConditionalSkip:
    def test_url_pattern_filter_applies_to_matching_path(self):
        app = _make_app(ApiOnlyFilter())
        client = TestClient(app)
        resp = client.get("/api/data")
        assert resp.headers.get("X-Api-Filter") == "applied"

    def test_url_pattern_filter_skipped_for_non_matching_path(self):
        app = _make_app(ApiOnlyFilter())
        client = TestClient(app)
        resp = client.get("/health")
        assert "X-Api-Filter" not in resp.headers


class TestFilterChainShortCircuit:
    def test_short_circuit_returns_early(self):
        """ShortCircuitFilter returns 429 without calling the route handler."""
        app = _make_app(ShortCircuitFilter())
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 429
        assert resp.json() == {"error": "rate limited"}


class TestFilterChainEmpty:
    def test_no_filters_passes_through(self):
        app = _make_app()  # no filters
        client = TestClient(app)
        resp = client.get("/test")
        assert resp.status_code == 200
        assert resp.text == "OK"


class TestFilterChainPathsend:
    """Tests for ASGI pathsend extension (used by Granian for FileResponse)."""

    @pytest.mark.asyncio
    async def test_pathsend_file_content_captured(self, tmp_path):
        """Middleware should read file content when receiving http.response.pathsend."""
        file = tmp_path / "hello.txt"
        file.write_text("file content here")

        async def pathsend_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/plain"),
                    ],
                }
            )
            await send({"type": "http.response.pathsend", "path": str(file)})

        mw = WebFilterChainMiddleware(app=pathsend_app, filters=[HeaderFilter()])

        sent_messages: list[dict] = []

        async def capture_send(message):
            sent_messages.append(message)

        scope = {"type": "http", "method": "GET", "path": "/file", "query_string": b"", "headers": []}

        async def receive():
            return {"type": "http.request", "body": b""}

        await mw(scope, receive, capture_send)

        start_msg = sent_messages[0]
        assert start_msg["type"] == "http.response.start"
        assert start_msg["status"] == 200

        body_msg = sent_messages[1]
        assert body_msg["type"] == "http.response.body"
        assert body_msg["body"] == b"file content here"

        # Verify filter headers were applied
        headers = dict(start_msg["headers"])
        assert headers[b"x-filter-a"] == b"applied"


class TestFilterChainStreaming:
    """Streaming responses (SSE, large downloads) must pass through incrementally,
    not be buffered until the generator completes."""

    @pytest.mark.asyncio
    async def test_streaming_response_forwarded_incrementally(self):
        """First chunk must reach the client BEFORE the generator produces the last one."""
        release = asyncio.Event()
        chunk1_seen = asyncio.Event()

        async def streaming_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send({"type": "http.response.body", "body": b"chunk1", "more_body": True})
            await release.wait()  # block until the test confirms chunk1 was delivered
            await send({"type": "http.response.body", "body": b"chunk2", "more_body": False})

        mw = WebFilterChainMiddleware(app=streaming_app, filters=[HeaderFilter()])

        sent: list[dict] = []

        async def capture_send(message):
            sent.append(message)
            if message.get("type") == "http.response.body" and message.get("body") == b"chunk1":
                chunk1_seen.set()

        scope = {"type": "http", "method": "GET", "path": "/sse", "query_string": b"", "headers": []}

        async def receive():
            return {"type": "http.request", "body": b""}

        task = asyncio.create_task(mw(scope, receive, capture_send))

        # If the middleware buffers the whole response, this times out (the second
        # chunk is gated behind ``release`` which we have not set yet).
        await asyncio.wait_for(chunk1_seen.wait(), timeout=2.0)

        start_msgs = [m for m in sent if m["type"] == "http.response.start"]
        assert start_msgs, "start message must be flushed before the body streams"
        body_msgs = [m for m in sent if m["type"] == "http.response.body"]
        assert body_msgs[0]["body"] == b"chunk1"
        assert body_msgs[0]["more_body"] is True

        release.set()
        await asyncio.wait_for(task, timeout=2.0)

        assert sent[-1]["body"] == b"chunk2"
        assert sent[-1]["more_body"] is False

    @pytest.mark.asyncio
    async def test_non_streaming_response_still_buffered_and_filtered(self):
        """Single-shot responses keep working and filters can still mutate headers."""

        async def json_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": False})

        mw = WebFilterChainMiddleware(app=json_app, filters=[HeaderFilter()])
        sent: list[dict] = []

        async def capture_send(message):
            sent.append(message)

        scope = {"type": "http", "method": "GET", "path": "/data", "query_string": b"", "headers": []}

        async def receive():
            return {"type": "http.request", "body": b""}

        await mw(scope, receive, capture_send)

        start = sent[0]
        assert start["type"] == "http.response.start"
        assert dict(start["headers"])[b"x-filter-a"] == b"applied"
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert body == b'{"ok":true}'


class TestFilterChainCustomDiscovery:
    @pytest.mark.asyncio
    async def test_custom_filter_auto_discovered_in_create_app(self):
        """Integration test: a @component WebFilter bean gets auto-discovered."""
        from pyfly.container.ordering import order as order_decorator
        from pyfly.container.stereotypes import component
        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.starlette.app import create_app

        @component
        @order_decorator(100)
        class CustomHeaderFilter(OncePerRequestFilter):
            async def do_filter(self, request, call_next):
                response = await call_next(request)
                response.headers["X-Custom"] = "hello"
                return response

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(CustomHeaderFilter)
        await ctx.start()

        app = create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)

        resp = client.get("/nonexistent", follow_redirects=False)
        # The filter chain still runs even for 404 routes
        assert resp.headers.get("X-Custom") == "hello"
