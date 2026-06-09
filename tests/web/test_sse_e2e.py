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
"""Server-Sent Events (SSE) end-to-end tests.

Exercises the full SSE stack:
- ``@sse_mapping`` decorator marks a method as an SSE endpoint.
- ``SSERegistrar._make_lazy_handler`` wraps it in a Starlette ``Route``.
- ``make_sse_response`` wraps the async generator in a ``StreamingResponse``
  with the correct SSE headers.
- ``format_sse_event`` serialises each yielded value.

The tests consume the stream via ``httpx.ASGITransport`` so no live server
port is needed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.web.sse.adapters.starlette import SSERegistrar, make_sse_response
from pyfly.web.sse.decorators import sse_mapping
from pyfly.web.sse.response import format_sse_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_from_controller(controller: Any) -> Starlette:
    """Build a Starlette app wired through SSERegistrar's lazy handler.

    Uses ``SSERegistrar._make_lazy_handler`` directly (the same factory used
    at runtime) so this is a real end-to-end test through the registrar.
    """

    class _FakeCtx:
        def get_bean(self, cls: type) -> Any:
            return controller

    # Discover the first @sse_mapping method on the controller
    for attr_name in dir(type(controller)):
        method_obj = getattr(type(controller), attr_name, None)
        if method_obj is not None and hasattr(method_obj, "__pyfly_sse_mapping__"):
            sse_meta: dict[str, Any] = method_obj.__pyfly_sse_mapping__
            full_path = sse_meta["path"] or "/events"
            handler = SSERegistrar._make_lazy_handler(_FakeCtx(), type(controller), attr_name)
            return Starlette(routes=[Route(full_path, handler, methods=["GET"])])

    raise RuntimeError("No @sse_mapping method found on controller")  # pragma: no cover


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


class _CounterController:
    """Yields a fixed sequence of integer counter events."""

    @sse_mapping(path="/counter")
    async def stream(self) -> AsyncGenerator[dict[str, Any], None]:  # type: ignore[return]
        for i in range(1, 4):
            yield {"count": i}


class _StringController:
    """Yields pre-formatted SSE strings (pass-through path)."""

    @sse_mapping(path="/strings")
    async def stream(self) -> AsyncGenerator[str, None]:  # type: ignore[return]
        for msg in ("hello", "world", "bye"):
            yield format_sse_event(msg, event="message")


class _SlowController:
    """Yields events with a brief delay to verify streaming (not buffering)."""

    @sse_mapping(path="/slow")
    async def stream(self) -> AsyncGenerator[dict[str, Any], None]:  # type: ignore[return]
        yield {"seq": 1}
        await asyncio.sleep(0.01)
        yield {"seq": 2}


# ---------------------------------------------------------------------------
# Tests — via httpx.ASGITransport (no live port)
# ---------------------------------------------------------------------------


class TestSseE2ETransport:
    """SSE stream consumed via httpx ASGI transport."""

    @pytest.mark.asyncio
    async def test_dict_events_are_formatted(self) -> None:
        """Each yielded dict is JSON-serialised and wrapped in ``data:`` lines."""
        app = _make_app_from_controller(_CounterController())

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", "/counter") as resp,
        ):
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            raw = await resp.aread()

        text = raw.decode()
        # Three events: count=1, count=2, count=3
        assert 'data: {"count": 1}' in text
        assert 'data: {"count": 2}' in text
        assert 'data: {"count": 3}' in text

    @pytest.mark.asyncio
    async def test_string_events_pass_through(self) -> None:
        """Pre-formatted SSE strings are forwarded as-is."""
        app = _make_app_from_controller(_StringController())

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", "/strings") as resp,
        ):
            assert resp.status_code == 200
            raw = await resp.aread()

        text = raw.decode()
        assert "event: message" in text
        assert "data: hello" in text
        assert "data: world" in text
        assert "data: bye" in text

    @pytest.mark.asyncio
    async def test_sse_headers_present(self) -> None:
        """Response must carry the standard SSE no-cache headers."""
        app = _make_app_from_controller(_CounterController())

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", "/counter") as resp,
        ):
            assert resp.headers.get("cache-control") == "no-cache"
            assert resp.headers.get("x-accel-buffering") == "no"

    @pytest.mark.asyncio
    async def test_slow_generator_still_streams_all_events(self) -> None:
        """Delayed generator still delivers all events to the client."""
        app = _make_app_from_controller(_SlowController())

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", "/slow") as resp,
        ):
            raw = await resp.aread()

        text = raw.decode()
        assert 'data: {"seq": 1}' in text
        assert 'data: {"seq": 2}' in text

    @pytest.mark.asyncio
    async def test_stream_terminates(self) -> None:
        """Finite generator: stream must complete without hanging."""
        app = _make_app_from_controller(_CounterController())

        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            client.stream("GET", "/counter") as resp,
        ):
            chunks = [chunk async for chunk in resp.aiter_bytes()]

        # All three events plus the trailing double-newlines
        joined = b"".join(chunks).decode()
        assert joined.count("data:") == 3


# ---------------------------------------------------------------------------
# Tests — via Starlette TestClient (synchronous convenience path)
# ---------------------------------------------------------------------------


class TestSseE2ETestClient:
    """SSE stream consumed via Starlette's synchronous TestClient."""

    def test_counter_stream_via_testclient(self) -> None:
        """Verify SSE frames arrive in order through the TestClient streaming path."""
        app = _make_app_from_controller(_CounterController())

        with TestClient(app) as client, client.stream("GET", "/counter") as resp:
            assert resp.status_code == 200
            raw = resp.read()

        text = raw.decode()
        # Events must appear in ascending order
        pos1 = text.index('data: {"count": 1}')
        pos2 = text.index('data: {"count": 2}')
        pos3 = text.index('data: {"count": 3}')
        assert pos1 < pos2 < pos3

    def test_make_sse_response_direct(self) -> None:
        """Unit-level: ``make_sse_response`` wraps a generator into StreamingResponse."""
        from starlette.responses import StreamingResponse

        async def gen() -> AsyncGenerator[str, None]:
            yield format_sse_event("ping", event="heartbeat")
            yield format_sse_event("pong", event="heartbeat")

        response = make_sse_response(gen())
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"
