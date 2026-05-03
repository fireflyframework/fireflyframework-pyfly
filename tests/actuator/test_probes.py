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
"""Tests for Kubernetes liveness/readiness probe groups."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
from pyfly.actuator.endpoints.health_endpoint import HealthEndpoint
from pyfly.actuator.health import HealthAggregator, HealthStatus, ProbeGroup
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


def _make_client(agg: HealthAggregator) -> TestClient:
    registry = ActuatorRegistry()
    registry.register(HealthEndpoint(agg))
    routes = make_starlette_actuator_routes(registry)
    app = Starlette(routes=routes)
    return TestClient(app)


# ---------------------------------------------------------------------------
# HealthAggregator probe group filtering
# ---------------------------------------------------------------------------


class TestProbeGroupFiltering:
    @pytest.mark.asyncio
    async def test_default_indicator_appears_in_all_probes(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("db", AlwaysUpIndicator())

        general = await agg.check()
        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert "db" in general.components
        assert "db" in liveness.components
        assert "db" in readiness.components

    @pytest.mark.asyncio
    async def test_liveness_only_indicator_in_liveness_only(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("ping", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})

        general = await agg.check()
        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert "ping" in general.components
        assert "ping" in liveness.components
        assert "ping" not in readiness.components

    @pytest.mark.asyncio
    async def test_readiness_only_indicator_in_readiness_only(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("cache", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})

        general = await agg.check()
        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert "cache" in general.components
        assert "cache" not in liveness.components
        assert "cache" in readiness.components

    @pytest.mark.asyncio
    async def test_indicator_with_both_groups_appears_in_both(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("core", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS, ProbeGroup.READINESS})

        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert "core" in liveness.components
        assert "core" in readiness.components

    @pytest.mark.asyncio
    async def test_down_liveness_does_not_affect_readiness(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("live-check", AlwaysDownIndicator(), groups={ProbeGroup.LIVENESS})
        agg.add_indicator("ready-check", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})

        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert liveness.status == "DOWN"
        assert readiness.status == "UP"

    @pytest.mark.asyncio
    async def test_down_readiness_does_not_affect_liveness(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("live-check", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})
        agg.add_indicator("ready-check", AlwaysDownIndicator(), groups={ProbeGroup.READINESS})

        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert liveness.status == "UP"
        assert readiness.status == "DOWN"

    @pytest.mark.asyncio
    async def test_empty_indicators_all_probes_up(self) -> None:
        agg = HealthAggregator()

        general = await agg.check()
        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert general.status == "UP"
        assert liveness.status == "UP"
        assert readiness.status == "UP"

    @pytest.mark.asyncio
    async def test_mixed_groups_and_defaults(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("default", AlwaysUpIndicator())
        agg.add_indicator("live-only", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})
        agg.add_indicator("ready-only", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})

        general = await agg.check()
        liveness = await agg.check_liveness()
        readiness = await agg.check_readiness()

        assert set(general.components.keys()) == {"default", "live-only", "ready-only"}
        assert set(liveness.components.keys()) == {"default", "live-only"}
        assert set(readiness.components.keys()) == {"default", "ready-only"}


# ---------------------------------------------------------------------------
# HealthEndpoint probe handlers
# ---------------------------------------------------------------------------


class TestHealthEndpointProbes:
    @pytest.mark.asyncio
    async def test_handle_liveness_returns_correct_data(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})
        ep = HealthEndpoint(agg)

        data = await ep.handle_liveness()
        assert data["status"] == "UP"
        assert "svc" in data["components"]

    @pytest.mark.asyncio
    async def test_handle_readiness_returns_correct_data(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})
        ep = HealthEndpoint(agg)

        data = await ep.handle_readiness()
        assert data["status"] == "UP"
        assert "svc" in data["components"]

    @pytest.mark.asyncio
    async def test_liveness_status_code_200_when_up(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})
        ep = HealthEndpoint(agg)

        assert await ep.get_liveness_status_code() == 200

    @pytest.mark.asyncio
    async def test_liveness_status_code_503_when_down(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysDownIndicator(), groups={ProbeGroup.LIVENESS})
        ep = HealthEndpoint(agg)

        assert await ep.get_liveness_status_code() == 503

    @pytest.mark.asyncio
    async def test_readiness_status_code_200_when_up(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})
        ep = HealthEndpoint(agg)

        assert await ep.get_readiness_status_code() == 200

    @pytest.mark.asyncio
    async def test_readiness_status_code_503_when_down(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysDownIndicator(), groups={ProbeGroup.READINESS})
        ep = HealthEndpoint(agg)

        assert await ep.get_readiness_status_code() == 503


# ---------------------------------------------------------------------------
# Starlette route integration
# ---------------------------------------------------------------------------


class TestProbeRoutes:
    def test_liveness_route_200(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.LIVENESS})
        client = _make_client(agg)

        resp = client.get("/actuator/health/liveness")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_liveness_route_503(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysDownIndicator(), groups={ProbeGroup.LIVENESS})
        client = _make_client(agg)

        resp = client.get("/actuator/health/liveness")
        assert resp.status_code == 503
        assert resp.json()["status"] == "DOWN"

    def test_readiness_route_200(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})
        client = _make_client(agg)

        resp = client.get("/actuator/health/readiness")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_readiness_route_503(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysDownIndicator(), groups={ProbeGroup.READINESS})
        client = _make_client(agg)

        resp = client.get("/actuator/health/readiness")
        assert resp.status_code == 503
        assert resp.json()["status"] == "DOWN"

    def test_overall_health_still_works(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("svc", AlwaysUpIndicator())
        client = _make_client(agg)

        resp = client.get("/actuator/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_index_includes_probe_links(self) -> None:
        agg = HealthAggregator()
        client = _make_client(agg)

        resp = client.get("/actuator")
        links = resp.json()["_links"]
        assert "health/liveness" in links
        assert links["health/liveness"]["href"] == "/actuator/health/liveness"
        assert "health/readiness" in links
        assert links["health/readiness"]["href"] == "/actuator/health/readiness"

    def test_probe_isolation_via_routes(self) -> None:
        agg = HealthAggregator()
        agg.add_indicator("live-only", AlwaysDownIndicator(), groups={ProbeGroup.LIVENESS})
        agg.add_indicator("ready-only", AlwaysUpIndicator(), groups={ProbeGroup.READINESS})
        client = _make_client(agg)

        liveness_resp = client.get("/actuator/health/liveness")
        readiness_resp = client.get("/actuator/health/readiness")
        overall_resp = client.get("/actuator/health")

        assert liveness_resp.status_code == 503
        assert readiness_resp.status_code == 200
        assert overall_resp.status_code == 503
