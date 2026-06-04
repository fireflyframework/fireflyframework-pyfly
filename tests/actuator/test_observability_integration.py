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
"""End-to-end Spring Boot observability parity through the real create_app path.

Exercises the whole chain: a controller with a templated route, HTTP
auto-instrumentation, the Prometheus scrape endpoint, the Micrometer-shaped
/actuator/metrics JSON, process meters, health, and info — all reachable via the
Spring-style exposure config.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.filters import metrics_filter as mf
from pyfly.web.mappings import get_mapping, request_mapping
from pyfly.web.params import PathVar


@rest_controller
@request_mapping("/users")
class UserController:
    @get_mapping("/{user_id}")
    async def get_user(self, user_id: PathVar[str]) -> dict:
        return {"id": user_id}


@pytest.fixture(autouse=True)
def _fresh_collectors():
    mf.reset_collectors()
    yield
    mf.reset_collectors()


async def _client() -> TestClient:
    cfg = Config(
        {
            "pyfly": {
                "app": {"name": "obs-demo", "version": "9.9.9"},
                "management": {"endpoints": {"web": {"exposure": {"include": "*"}}}},
            }
        }
    )
    ctx = ApplicationContext(cfg)
    ctx.register_bean(UserController)
    await ctx.start()
    app = create_app(context=ctx, docs_enabled=False)
    return TestClient(app)


class TestObservabilityEndToEnd:
    @pytest.mark.asyncio
    async def test_auto_instrumentation_templated_uri_in_prometheus(self):
        client = await _client()
        # Drive two requests on the same templated route.
        assert client.get("/users/42").status_code == 200
        assert client.get("/users/99").status_code == 200

        body = client.get("/actuator/prometheus").text
        # Spring/Micrometer meter name + templated, cardinality-safe uri tag.
        assert "http_server_requests_seconds_count" in body
        assert 'uri="/users/{user_id}"' in body
        assert 'uri="/users/42"' not in body
        assert "http_server_requests_seconds_max" in body
        # Process/system meters with Micrometer names.
        assert "process_uptime_seconds" in body
        assert "system_cpu_count" in body

    @pytest.mark.asyncio
    async def test_metrics_list_and_detail_micrometer_shape(self):
        client = await _client()
        client.get("/users/7")

        names = client.get("/actuator/metrics").json()["names"]
        assert "http.server.requests" in names

        detail = client.get("/actuator/metrics/http.server.requests").json()
        assert detail["name"] == "http.server.requests"
        assert detail["baseUnit"] == "seconds"
        stats = {m["statistic"] for m in detail["measurements"]}
        assert "COUNT" in stats
        assert "TOTAL_TIME" in stats
        tags = {t["tag"] for t in detail["availableTags"]}
        assert {"method", "uri", "status", "outcome", "exception"} <= tags
        uri_values = next(t["values"] for t in detail["availableTags"] if t["tag"] == "uri")
        assert "/users/{user_id}" in uri_values

    @pytest.mark.asyncio
    async def test_metrics_tag_drilldown(self):
        client = await _client()
        client.get("/users/7")
        detail = client.get("/actuator/metrics/http.server.requests?tag=method:GET").json()
        assert any(m["statistic"] == "COUNT" and m["value"] >= 1 for m in detail["measurements"])

    @pytest.mark.asyncio
    async def test_health_and_info_default_exposed(self):
        client = await _client()
        health = client.get("/actuator/health")
        assert health.status_code == 200
        assert health.json()["status"] == "UP"

        info = client.get("/actuator/info").json()
        assert info["app"]["name"] == "obs-demo"
        assert info["app"]["version"] == "9.9.9"

    @pytest.mark.asyncio
    async def test_prometheus_scrape_endpoint_excluded_from_its_own_metric(self):
        client = await _client()
        client.get("/actuator/prometheus")
        body = client.get("/actuator/prometheus").text
        # The scrape endpoint itself is not instrumented (excluded).
        assert 'uri="/actuator/prometheus"' not in body
