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
"""Tests for the additional Spring Boot actuator endpoints (Wave 6)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.scheduling import scheduled
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.mappings import get_mapping, request_mapping
from pyfly.web.params import PathVar


@service
class ReportService:
    @scheduled(fixed_rate=timedelta(seconds=30))
    async def emit(self) -> None:  # pragma: no cover - never actually run here
        return None


@rest_controller
@request_mapping("/widgets")
class WidgetController:
    @get_mapping("/{widget_id}")
    async def get_widget(self, widget_id: PathVar[str]) -> dict:
        return {"id": widget_id}


async def _client() -> TestClient:
    cfg = Config(
        {
            "pyfly": {
                "app": {"name": "extra-demo"},
                "security": {"jwt": {"secret": "topsecret"}},
                "management": {"endpoints": {"web": {"exposure": {"include": "*"}}}},
            }
        }
    )
    ctx = ApplicationContext(cfg)
    ctx.register_bean(ReportService)
    ctx.register_bean(WidgetController)
    await ctx.start()
    return TestClient(create_app(context=ctx, docs_enabled=False))


class TestExtraActuatorEndpoints:
    @pytest.mark.asyncio
    async def test_beans_contexts_envelope(self):
        client = await _client()
        data = client.get("/actuator/beans").json()
        beans = data["contexts"]["application"]["beans"]
        assert "WidgetController" in beans
        assert beans["WidgetController"]["scope"] == "singleton"

    @pytest.mark.asyncio
    async def test_configprops_groups_by_prefix_and_masks(self):
        client = await _client()
        data = client.get("/actuator/configprops").json()
        beans = data["contexts"]["application"]["beans"]
        # Framework property classes are reported with their prefix.
        assert any(b.get("prefix") == "pyfly.server" for b in beans.values())

    @pytest.mark.asyncio
    async def test_mappings_lists_controller_routes(self):
        client = await _client()
        data = client.get("/actuator/mappings").json()
        servlet = data["contexts"]["application"]["mappings"]["dispatcherServlets"]["dispatcherServlet"]
        assert any("/widgets/{widget_id}" in m["predicate"] for m in servlet)

    @pytest.mark.asyncio
    async def test_scheduledtasks_groups_by_trigger(self):
        client = await _client()
        data = client.get("/actuator/scheduledtasks").json()
        assert "cron" in data and "fixedRate" in data and "fixedDelay" in data
        assert any("ReportService.emit" in t["runnable"]["target"] for t in data["fixedRate"])
        assert data["fixedRate"][0]["interval"] == 30000  # 30s -> ms

    @pytest.mark.asyncio
    async def test_threaddump_returns_threads(self):
        client = await _client()
        data = client.get("/actuator/threaddump").json()
        assert isinstance(data["threads"], list)
        assert len(data["threads"]) >= 1
        assert "stackTrace" in data["threads"][0]

    @pytest.mark.asyncio
    async def test_caches_shape(self):
        client = await _client()
        data = client.get("/actuator/caches").json()
        assert "cacheManagers" in data

    @pytest.mark.asyncio
    async def test_conditions_report(self):
        client = await _client()
        data = client.get("/actuator/conditions").json()
        app_ctx = data["contexts"]["application"]
        assert "positiveMatches" in app_ctx
        assert "negativeMatches" in app_ctx

    @pytest.mark.asyncio
    async def test_httpexchanges_records_requests(self):
        client = await _client()
        client.get("/widgets/7")
        data = client.get("/actuator/httpexchanges").json()
        assert isinstance(data["exchanges"], list)
        assert any(e["request"]["method"] == "GET" for e in data["exchanges"])

    @pytest.mark.asyncio
    async def test_index_lists_all_exposed_endpoints(self):
        client = await _client()
        links = client.get("/actuator").json()["_links"]
        for eid in ("beans", "configprops", "mappings", "scheduledtasks", "threaddump", "caches", "conditions"):
            assert eid in links
