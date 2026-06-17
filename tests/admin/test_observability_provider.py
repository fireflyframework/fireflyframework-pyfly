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
"""Tests for the admin ObservabilityProvider."""

from __future__ import annotations

import pytest

from pyfly.admin.providers.observability_provider import ObservabilityProvider
from pyfly.observability import server_metrics as sm
from pyfly.web.adapters.starlette import asgi_server_metrics as asm


@pytest.fixture(autouse=True)
def _fresh_collectors():
    asm.reset_collectors()
    sm.reset_collectors()
    yield
    asm.reset_collectors()
    sm.reset_collectors()


class TestObservabilityProvider:
    async def test_snapshot_shape_with_no_metrics(self) -> None:
        snap = await ObservabilityProvider().get_observability()
        # Shape contract holds even with nothing registered yet.
        for key in (
            "timestamp",
            "available",
            "multiprocess",
            "server",
            "workers",
            "uptime_seconds",
            "in_flight_requests",
            "requests_per_second",
            "per_worker",
            "lifecycle",
        ):
            assert key in snap
        assert snap["multiprocess"] is False
        assert snap["per_worker"] == []

    async def test_reads_registered_server_metrics(self) -> None:
        binder = sm.ServerMetricsBinder(server_name="uvicorn", workers=2, sample_interval=60)
        await binder.start()
        try:
            snap = await ObservabilityProvider().get_observability()
            assert snap["workers"] == 1  # one reporting worker (this process)
            assert snap["uptime_seconds"] >= 0.0
            assert snap["started_total"] >= 1
            assert len(snap["per_worker"]) == 1
            assert snap["per_worker"][0]["pid"] == str(__import__("os").getpid())
        finally:
            await binder.stop()

    async def test_requests_total_from_asgi_middleware(self) -> None:
        from pyfly.web.adapters.starlette.asgi_server_metrics import ServerMetricsASGIMiddleware

        async def _noop(scope, receive, send):  # noqa: ANN001
            pass

        mw = ServerMetricsASGIMiddleware(_noop)
        await mw({"type": "http", "path": "/x"}, lambda: None, lambda m: None)

        snap = await ObservabilityProvider().get_observability()
        assert snap["requests_total"] == 1


class TestObservabilityStream:
    async def test_stream_emits_observability_event(self) -> None:
        import json

        from pyfly.admin.api.sse import observability_stream

        gen = observability_stream(ObservabilityProvider(), interval=0.01)
        try:
            event = await anext(gen)
        finally:
            await gen.aclose()

        assert "event: observability" in event
        payload = json.loads(next(line[len("data: ") :] for line in event.splitlines() if line.startswith("data: ")))
        assert payload["available"] is True
        assert "server" in payload

