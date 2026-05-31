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
"""Regression tests for admin dashboard wiring inside ``create_app``.

These cover the integration seam that unit tests of the route builder miss:
the HTTP trace collector must be created *and* inserted into the live
WebFilter chain so that real traffic is recorded, and the server provider
must resolve its adapter lazily (the adapter bean is instantiated by
``ApplicationContext.start()`` which runs after the app is assembled).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


async def _admin_context() -> ApplicationContext:
    ctx = ApplicationContext(Config({"pyfly": {"admin": {"enabled": True}}}))
    await ctx.start()
    return ctx


class TestAdminTraceCollectorWiring:
    @pytest.mark.asyncio
    async def test_traces_recorded_for_real_traffic(self):
        """A request through the app must show up in /admin/api/traces."""
        ctx = await _admin_context()
        app = create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)

        # Generate some non-admin traffic (admin/actuator paths are excluded).
        for _ in range(3):
            client.get("/some/business/path", follow_redirects=False)

        resp = client.get("/admin/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 3, f"expected recorded traces, got {data}"
        paths = {t["path"] for t in data["traces"]}
        assert "/some/business/path" in paths

    @pytest.mark.asyncio
    async def test_admin_and_actuator_paths_excluded_from_traces(self):
        ctx = await _admin_context()
        app = create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)

        client.get("/admin/api/overview")
        client.get("/admin/api/traces")

        resp = client.get("/admin/api/traces")
        data = resp.json()
        admin_paths = [t for t in data["traces"] if t["path"].startswith("/admin")]
        assert admin_paths == [], "admin paths must not be traced"

    @pytest.mark.asyncio
    async def test_server_info_endpoint_does_not_error(self):
        ctx = await _admin_context()
        app = create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)

        resp = client.get("/admin/api/server")
        assert resp.status_code == 200
        # Without a running ApplicationServerPort the provider returns the
        # graceful "unknown" shape rather than raising.
        assert "name" in resp.json()


class TestAdminSseStreams:
    """SSE streaming through the filter chain is covered deterministically at the
    ASGI level by tests/web/test_filter_chain.py::TestFilterChainStreaming. Here we
    only assert the admin SSE generator emits an initial event immediately (no
    buffering), driving the generator directly to avoid an unbounded TestClient
    stream that would never terminate."""

    @pytest.mark.asyncio
    async def test_health_stream_emits_initial_event(self):
        from pyfly.actuator.health import HealthAggregator
        from pyfly.admin.api.sse import health_stream
        from pyfly.admin.providers.health_provider import HealthProvider

        provider = HealthProvider(HealthAggregator())
        gen = health_stream(provider, interval=0.01)
        first = await gen.__anext__()
        await gen.aclose()
        assert first.startswith("event: health")
        assert "data:" in first and "status" in first
