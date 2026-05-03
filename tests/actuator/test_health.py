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
"""Tests for the actuator health system."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
from pyfly.actuator.endpoints import HealthEndpoint
from pyfly.actuator.health import HealthAggregator, HealthIndicator, HealthResult, HealthStatus
from pyfly.actuator.registry import ActuatorRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class AlwaysUpIndicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="UP")


class AlwaysDownIndicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="DOWN", details={"reason": "offline"})


class ExplodingIndicator:
    async def health(self) -> HealthStatus:
        raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# HealthStatus
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_creation_defaults(self):
        hs = HealthStatus(status="UP")
        assert hs.status == "UP"
        assert hs.details == {}

    def test_creation_with_details(self):
        hs = HealthStatus(status="DOWN", details={"db": "unreachable"})
        assert hs.status == "DOWN"
        assert hs.details == {"db": "unreachable"}


# ---------------------------------------------------------------------------
# HealthResult
# ---------------------------------------------------------------------------


class TestHealthResult:
    def test_to_dict_no_components(self):
        hr = HealthResult(status="UP")
        assert hr.to_dict() == {"status": "UP"}

    def test_to_dict_with_components(self):
        hr = HealthResult(
            status="DOWN",
            components={"db": HealthStatus(status="DOWN", details={"error": "timeout"})},
        )
        d = hr.to_dict()
        assert d["status"] == "DOWN"
        assert d["components"]["db"]["status"] == "DOWN"
        assert d["components"]["db"]["details"] == {"error": "timeout"}


# ---------------------------------------------------------------------------
# HealthIndicator protocol
# ---------------------------------------------------------------------------


class TestHealthIndicatorProtocol:
    def test_up_indicator_is_instance(self):
        assert isinstance(AlwaysUpIndicator(), HealthIndicator)


# ---------------------------------------------------------------------------
# HealthAggregator
# ---------------------------------------------------------------------------


class TestHealthAggregator:
    @pytest.mark.asyncio
    async def test_no_indicators_returns_up(self):
        agg = HealthAggregator()
        result = await agg.check()
        assert result.status == "UP"
        assert result.components == {}

    @pytest.mark.asyncio
    async def test_all_up(self):
        agg = HealthAggregator()
        agg.add_indicator("svc1", AlwaysUpIndicator())
        agg.add_indicator("svc2", AlwaysUpIndicator())
        result = await agg.check()
        assert result.status == "UP"
        assert "svc1" in result.components
        assert "svc2" in result.components

    @pytest.mark.asyncio
    async def test_one_down_means_overall_down(self):
        agg = HealthAggregator()
        agg.add_indicator("svc1", AlwaysUpIndicator())
        agg.add_indicator("db", AlwaysDownIndicator())
        result = await agg.check()
        assert result.status == "DOWN"
        assert result.components["db"].status == "DOWN"

    @pytest.mark.asyncio
    async def test_exception_treated_as_down(self):
        agg = HealthAggregator()
        agg.add_indicator("flaky", ExplodingIndicator())
        result = await agg.check()
        assert result.status == "DOWN"
        assert result.components["flaky"].status == "DOWN"
        assert "error" in result.components["flaky"].details


# ---------------------------------------------------------------------------
# /actuator/health endpoint
# ---------------------------------------------------------------------------


def _make_health_routes(agg):
    registry = ActuatorRegistry()
    registry.register(HealthEndpoint(agg))
    return make_starlette_actuator_routes(registry)


class TestHealthEndpoint:
    def _make_client(self, aggregator: HealthAggregator) -> TestClient:
        routes = _make_health_routes(aggregator)
        app = Starlette(routes=routes)
        return TestClient(app)

    def test_200_when_up(self):
        agg = HealthAggregator()
        client = self._make_client(agg)
        resp = client.get("/actuator/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    @pytest.mark.asyncio
    async def test_503_when_down(self):
        agg = HealthAggregator()
        agg.add_indicator("db", AlwaysDownIndicator())
        client = self._make_client(agg)
        resp = client.get("/actuator/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "DOWN"
