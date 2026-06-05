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
"""Regression tests for admin fixes.

#66 — require_auth / allowed_roles enforced on every admin API route.
#69 — TRACE / OFF logger levels apply instead of silently failing.
#71 — the beans SSE stream is reachable.
"""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.admin.adapters.starlette import AdminRouteBuilder
from pyfly.admin.config import AdminProperties
from pyfly.admin.providers.beans_provider import BeansProvider
from pyfly.admin.providers.cache_provider import CacheProvider
from pyfly.admin.providers.config_provider import ConfigProvider
from pyfly.admin.providers.cqrs_provider import CqrsProvider
from pyfly.admin.providers.env_provider import EnvProvider
from pyfly.admin.providers.health_provider import HealthProvider
from pyfly.admin.providers.logfile_provider import LogfileProvider
from pyfly.admin.providers.loggers_provider import LoggersProvider
from pyfly.admin.providers.mappings_provider import MappingsProvider
from pyfly.admin.providers.metrics_provider import MetricsProvider
from pyfly.admin.providers.overview_provider import OverviewProvider
from pyfly.admin.providers.scheduled_provider import ScheduledProvider
from pyfly.admin.providers.traces_provider import TracesProvider
from pyfly.admin.providers.transactions_provider import TransactionsProvider
from pyfly.admin.registry import AdminViewRegistry
from pyfly.context.request_context import RequestContext
from pyfly.security.context import SecurityContext
from tests.admin.test_providers import _make_mock_context


def _make_builder(props: AdminProperties) -> AdminRouteBuilder:
    ctx = _make_mock_context()
    ctx.config.to_dict.return_value = {"pyfly": {"app": {"name": "test"}}}
    ctx.config.loaded_sources = []
    return AdminRouteBuilder(
        properties=props,
        overview=OverviewProvider(ctx, None),
        beans=BeansProvider(ctx),
        health=HealthProvider(None),
        env=EnvProvider(ctx),
        config=ConfigProvider(ctx),
        loggers=LoggersProvider(),
        metrics=MetricsProvider(),
        scheduled=ScheduledProvider(ctx),
        mappings=MappingsProvider(ctx),
        caches=CacheProvider(ctx),
        cqrs=CqrsProvider(ctx),
        transactions=TransactionsProvider(ctx),
        traces=TracesProvider(None),
        view_registry=AdminViewRegistry(),
        logfile=LogfileProvider(ctx),
    )


def _client(props: AdminProperties) -> TestClient:
    return TestClient(Starlette(routes=_make_builder(props).build_routes()))


# ---------------------------------------------------------------------------
# #66 — auth enforcement
# ---------------------------------------------------------------------------


class TestAdminAuth:
    def test_api_open_when_auth_disabled(self):
        client = _client(AdminProperties(require_auth=False))
        assert client.get("/admin/api/overview").status_code == 200

    def test_api_blocked_when_auth_required_and_anonymous(self):
        client = _client(AdminProperties(require_auth=True))
        # No security context populated (no auth filter) -> 401 on every API route.
        assert client.get("/admin/api/overview").status_code == 401
        assert client.post("/admin/api/caches/x/evict").status_code == 401
        assert client.get("/admin/api/settings").status_code == 401

    def test_spa_shell_public_even_with_auth(self):
        client = _client(AdminProperties(require_auth=True))
        # The SPA shell must stay reachable so the dashboard can boot.
        assert client.get("/admin").status_code != 401

    def test_auth_failure_logic(self):
        builder = _make_builder(AdminProperties(require_auth=True, allowed_roles=["ADMIN"]))
        try:
            rc = RequestContext.init()
            rc.security_context = SecurityContext(user_id="u", roles=["ADMIN"])
            assert builder._auth_failure() is None

            rc.security_context = SecurityContext(user_id="u", roles=["USER"])
            denied = builder._auth_failure()
            assert denied is not None and denied.status_code == 403

            rc.security_context = None
            denied = builder._auth_failure()
            assert denied is not None and denied.status_code == 401
        finally:
            RequestContext.clear()

    def test_auth_failure_noop_when_disabled(self):
        builder = _make_builder(AdminProperties(require_auth=False))
        assert builder._auth_failure() is None


# ---------------------------------------------------------------------------
# #69 — TRACE / OFF logger levels
# ---------------------------------------------------------------------------


class TestLoggerLevels:
    @pytest.mark.asyncio
    async def test_trace_level_applies(self):
        provider = LoggersProvider()
        result = await provider.set_level("pyfly.test.trace", "TRACE")
        assert "error" not in result
        assert result["configuredLevel"] == "TRACE"
        assert logging.getLogger("pyfly.test.trace").level == 5

    @pytest.mark.asyncio
    async def test_off_level_disables(self):
        provider = LoggersProvider()
        result = await provider.set_level("pyfly.test.off", "OFF")
        assert "error" not in result
        assert logging.getLogger("pyfly.test.off").level == logging.CRITICAL + 1

    @pytest.mark.asyncio
    async def test_unknown_level_still_errors(self):
        result = await LoggersProvider().set_level("pyfly.test", "BOGUS")
        assert "error" in result

    def test_trace_off_round_trip_via_http(self):
        client = _client(AdminProperties())
        for level in ("TRACE", "OFF", "DEBUG"):
            resp = client.post("/admin/api/loggers/pyfly.demo", json={"level": level})
            assert resp.status_code == 200, level
            assert resp.json()["configuredLevel"] == level


# ---------------------------------------------------------------------------
# #71 — beans SSE route
# ---------------------------------------------------------------------------


class TestBeansSse:
    def test_beans_sse_route_registered(self):
        builder = _make_builder(AdminProperties())
        paths = {r.path for r in builder.build_routes() if hasattr(r, "path")}
        assert "/admin/api/sse/beans" in paths
