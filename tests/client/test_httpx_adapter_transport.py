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
"""Real-transport tests for HttpxClientAdapter using respx (no Docker).

respx intercepts the underlying httpx.AsyncClient globally, so the adapter's
internally-constructed client is mocked without any dependency injection.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import respx
from httpx import Request, Response

from pyfly.client.adapters.httpx_adapter import HttpxClientAdapter


@respx.mock
@pytest.mark.asyncio
async def test_get_request_reaches_mock_route() -> None:
    """Adapter GET request is intercepted by respx and returns expected data."""
    adapter = HttpxClientAdapter(base_url="https://api.test", timeout=timedelta(seconds=5))
    route = respx.get("https://api.test/users/1").mock(return_value=Response(200, json={"id": 1, "name": "Alice"}))

    resp = await adapter.request("GET", "/users/1")

    assert route.called
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "Alice"}
    await adapter.stop()


@respx.mock
@pytest.mark.asyncio
async def test_inject_headers_does_not_error() -> None:
    """inject_headers runs without error (traceparent may be absent without OTel)."""
    adapter = HttpxClientAdapter(base_url="https://api.test", timeout=timedelta(seconds=5))
    respx.get("https://api.test/ping").mock(return_value=Response(200, json={}))

    # The call should complete without raising even when no OTel span is active.
    resp = await adapter.request("GET", "/ping")
    assert resp.status_code == 200
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_closes_client() -> None:
    """stop() closes the underlying httpx client (aclose is idempotent)."""
    adapter = HttpxClientAdapter(base_url="https://api.test", timeout=timedelta(seconds=5))
    # stop() delegates to httpx.AsyncClient.aclose(); a second call must also succeed.
    await adapter.stop()
    await adapter.stop()


@respx.mock
@pytest.mark.asyncio
async def test_base_url_joining() -> None:
    """Adapter correctly joins base_url with relative path."""
    adapter = HttpxClientAdapter(base_url="https://api.test", timeout=timedelta(seconds=5))
    route = respx.get("https://api.test/v2/items").mock(return_value=Response(200, json={"items": []}))

    resp = await adapter.request("GET", "/v2/items")

    assert route.called
    assert resp.json() == {"items": []}
    await adapter.stop()


@respx.mock
@pytest.mark.asyncio
async def test_per_request_headers_merged() -> None:
    """Headers passed per-request are forwarded to the actual HTTP call."""
    adapter = HttpxClientAdapter(base_url="https://api.test", timeout=timedelta(seconds=5))

    captured_headers: dict[str, str] = {}

    def capture(request: Request, **kwargs: Any) -> Response:
        captured_headers.update(dict(request.headers))
        return Response(200, json={})

    respx.get("https://api.test/secure").mock(side_effect=capture)
    await adapter.request("GET", "/secure", headers={"X-Custom": "hello"})

    assert captured_headers.get("x-custom") == "hello"
    await adapter.stop()
