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
"""Tests for health-indicator wiring: protocol conformance, aggregation, show-details."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pyfly.actuator.health import HealthIndicator, HealthResult, HealthStatus, aggregate_status
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.cqrs.actuator.health import CqrsHealthIndicator
from pyfly.transactional.health import OrchestrationHealthIndicator
from pyfly.web.adapters.starlette.app import create_app


class TestStatusAggregation:
    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            ([], "UP"),
            (["UP", "UNKNOWN"], "UP"),
            (["UNKNOWN"], "UNKNOWN"),
            (["DOWN", "UP"], "DOWN"),
            (["OUT_OF_SERVICE", "UP"], "OUT_OF_SERVICE"),
            (["DOWN", "OUT_OF_SERVICE"], "DOWN"),
            (["DEGRADED"], "DOWN"),  # non-canonical -> treated as DOWN
        ],
    )
    def test_aggregate(self, statuses, expected):
        assert aggregate_status(statuses) == expected


class TestToDictShowFlags:
    def _result(self) -> HealthResult:
        return HealthResult(
            status="UP",
            components={"db": HealthStatus(status="UP", details={"database": "sqlite"})},
        )

    def test_full(self):
        d = self._result().to_dict()
        assert d["components"]["db"]["details"] == {"database": "sqlite"}

    def test_hide_details(self):
        d = self._result().to_dict(show_details=False)
        assert "details" not in d["components"]["db"]
        assert d["components"]["db"]["status"] == "UP"

    def test_hide_components(self):
        d = self._result().to_dict(show_components=False)
        assert "components" not in d


class _FakeRegistry:
    def __init__(self, commands: int, queries: int) -> None:
        self.command_handler_count = commands
        self.query_handler_count = queries


class _FakePersistence:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    async def is_healthy(self) -> bool:
        return self._healthy


class TestIndicatorProtocolConformance:
    @pytest.mark.asyncio
    async def test_cqrs_indicator_is_async_and_conforms(self):
        ind = CqrsHealthIndicator(_FakeRegistry(2, 1))
        assert isinstance(ind, HealthIndicator)
        result = await ind.health()
        assert isinstance(result, HealthStatus)
        assert result.status == "UP"
        assert result.details["command_handlers"] == 2

    @pytest.mark.asyncio
    async def test_cqrs_indicator_unknown_when_idle(self):
        result = await CqrsHealthIndicator(_FakeRegistry(0, 0)).health()
        assert result.status == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_orchestration_indicator_conforms(self):
        ind = OrchestrationHealthIndicator(_FakePersistence(healthy=True))
        assert isinstance(ind, HealthIndicator)
        result = await ind.health()
        assert isinstance(result, HealthStatus)
        assert result.status == "UP"

    @pytest.mark.asyncio
    async def test_orchestration_indicator_down(self):
        result = await OrchestrationHealthIndicator(_FakePersistence(healthy=False)).health()
        assert result.status == "DOWN"


class _OutOfServiceIndicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="OUT_OF_SERVICE", details={"reason": "draining"})


class TestHealthEndpointIntegration:
    @pytest.mark.asyncio
    async def test_out_of_service_maps_to_503(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(_OutOfServiceIndicator)
        ctx.container.bind(_OutOfServiceIndicator, _OutOfServiceIndicator)
        await ctx.start()

        client = TestClient(create_app(context=ctx), raise_server_exceptions=False)
        resp = client.get("/actuator/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "OUT_OF_SERVICE"

    @pytest.mark.asyncio
    async def test_show_details_never_hides_details(self):
        cfg = Config({"pyfly": {"management": {"endpoint": {"health": {"show-details": "never"}}}}})
        ctx = ApplicationContext(cfg)
        ctx.register_bean(_OutOfServiceIndicator)
        ctx.container.bind(_OutOfServiceIndicator, _OutOfServiceIndicator)
        await ctx.start()

        client = TestClient(create_app(context=ctx), raise_server_exceptions=False)
        body = client.get("/actuator/health").json()
        # components present (show-components default), but details hidden.
        comp = body["components"]["_OutOfServiceIndicator"]
        assert comp["status"] == "OUT_OF_SERVICE"
        assert "details" not in comp
