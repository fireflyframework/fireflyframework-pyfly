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
"""Tests for MetricsFilter — HTTP auto-instrumentation."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from prometheus_client import REGISTRY

from pyfly.web.adapters.starlette.filters.metrics_filter import MetricsFilter


def _make_request(method: str = "GET", path: str = "/api/users") -> MagicMock:
    req = MagicMock()
    req.method = method
    req.url.path = path
    return req


def _make_response(status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


@pytest.fixture(autouse=True)
def _clean_prometheus_registry():
    """Unregister MetricsFilter collectors between tests."""
    collectors_before = set(REGISTRY._names_to_collectors.keys())
    yield
    for name in list(REGISTRY._names_to_collectors.keys()):
        if name not in collectors_before:
            with contextlib.suppress(Exception):
                REGISTRY.unregister(REGISTRY._names_to_collectors[name])


class TestMetricsFilter:
    @pytest.mark.asyncio
    async def test_increments_request_counter(self) -> None:
        f = MetricsFilter()
        req = _make_request()
        resp = _make_response(200)
        call_next = AsyncMock(return_value=resp)

        await f.do_filter(req, call_next)

        sample = f._requests_total.labels(method="GET", path="/api/users", status="200")
        assert sample._value.get() == 1.0

    @pytest.mark.asyncio
    async def test_records_duration_histogram(self) -> None:
        f = MetricsFilter()
        req = _make_request()
        resp = _make_response(200)
        call_next = AsyncMock(return_value=resp)

        await f.do_filter(req, call_next)

        sample = f._request_duration.labels(method="GET", path="/api/users")
        assert sample._sum.get() > 0

    @pytest.mark.asyncio
    async def test_active_requests_gauge_returns_to_zero(self) -> None:
        f = MetricsFilter()
        req = _make_request()
        resp = _make_response(200)
        call_next = AsyncMock(return_value=resp)

        await f.do_filter(req, call_next)

        assert f._active_requests._value.get() == 0.0

    @pytest.mark.asyncio
    async def test_records_500_on_exception(self) -> None:
        f = MetricsFilter()
        req = _make_request("POST", "/api/orders")
        call_next = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await f.do_filter(req, call_next)

        sample = f._requests_total.labels(method="POST", path="/api/orders", status="500")
        assert sample._value.get() == 1.0

    def test_excludes_actuator_paths(self) -> None:
        f = MetricsFilter()
        req = _make_request("GET", "/actuator/health")
        assert f.should_not_filter(req) is True

    def test_does_not_exclude_api_paths(self) -> None:
        f = MetricsFilter()
        req = _make_request("GET", "/api/users")
        assert f.should_not_filter(req) is False
