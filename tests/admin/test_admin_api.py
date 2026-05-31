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
"""Integration tests for admin REST API."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.admin.adapters.starlette import AdminRouteBuilder
from pyfly.admin.config import AdminProperties
from pyfly.admin.log_handler import AdminLogHandler
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
from tests.admin.test_providers import _make_mock_context


@pytest.fixture
def log_handler():
    import logging

    handler = AdminLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


@pytest.fixture
def admin_client(log_handler):
    ctx = _make_mock_context()
    data = {"pyfly": {"app": {"name": "test"}, "web": {"port": 8080}}}
    ctx.config.to_dict.return_value = data
    ctx.config.loaded_sources = []

    # Register the log handler in the mock context so LogfileProvider can find it
    from enum import Enum
    from unittest.mock import MagicMock

    class _Scope(Enum):
        SINGLETON = "SINGLETON"

    handler_reg = MagicMock()
    handler_reg.instance = log_handler
    handler_reg.name = "adminLogHandler"
    handler_reg.scope = _Scope.SINGLETON
    ctx.container._registrations[AdminLogHandler] = handler_reg

    props = AdminProperties()

    builder = AdminRouteBuilder(
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
    routes = builder.build_routes()
    app = Starlette(routes=routes)
    return TestClient(app)


class TestAdminAPI:
    def test_overview(self, admin_client):
        resp = admin_client.get("/admin/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "app" in data
        assert "health" in data
        assert "beans" in data

    def test_beans(self, admin_client):
        resp = admin_client.get("/admin/api/beans")
        assert resp.status_code == 200
        data = resp.json()
        assert "beans" in data

    def test_health(self, admin_client):
        resp = admin_client.get("/admin/api/health")
        assert resp.status_code == 200

    def test_health_status_up_with_aggregator(self):
        """Health returns UP (not UNKNOWN) when a HealthAggregator is provided."""
        from pyfly.actuator.health import HealthAggregator

        ctx = _make_mock_context()
        data = {"pyfly": {"app": {"name": "test"}, "web": {"port": 8080}}}
        ctx.config.to_dict.return_value = data
        ctx.config.loaded_sources = []

        agg = HealthAggregator()
        builder = AdminRouteBuilder(
            properties=AdminProperties(),
            overview=OverviewProvider(ctx, agg),
            beans=BeansProvider(ctx),
            health=HealthProvider(agg),
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
        )
        app = Starlette(routes=builder.build_routes())
        client = TestClient(app)

        resp = client.get("/admin/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_loggers(self, admin_client):
        resp = admin_client.get("/admin/api/loggers")
        assert resp.status_code == 200
        data = resp.json()
        assert "ROOT" in data["loggers"]

    def test_settings(self, admin_client):
        resp = admin_client.get("/admin/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "PyFly Admin"

    def test_env(self, admin_client):
        resp = admin_client.get("/admin/api/env")
        assert resp.status_code == 200
        data = resp.json()
        assert "active_profiles" in data
        assert "properties" in data

    def test_config(self, admin_client):
        resp = admin_client.get("/admin/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data

    def test_scheduled(self, admin_client):
        resp = admin_client.get("/admin/api/scheduled")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data

    def test_mappings(self, admin_client):
        resp = admin_client.get("/admin/api/mappings")
        assert resp.status_code == 200
        data = resp.json()
        assert "mappings" in data

    def test_caches(self, admin_client):
        resp = admin_client.get("/admin/api/caches")
        assert resp.status_code == 200

    def test_cqrs(self, admin_client):
        resp = admin_client.get("/admin/api/cqrs")
        assert resp.status_code == 200
        data = resp.json()
        assert "handlers" in data

    def test_transactions(self, admin_client):
        resp = admin_client.get("/admin/api/transactions")
        assert resp.status_code == 200
        data = resp.json()
        assert "sagas" in data
        assert "tcc" in data
        assert "saga_count" in data
        assert "tcc_count" in data
        assert "total" in data
        assert "in_flight" in data

    def test_traces(self, admin_client):
        resp = admin_client.get("/admin/api/traces")
        assert resp.status_code == 200
        data = resp.json()
        assert "traces" in data

    def test_views(self, admin_client):
        resp = admin_client.get("/admin/api/views")
        assert resp.status_code == 200
        data = resp.json()
        assert "views" in data

    def test_metrics(self, admin_client):
        resp = admin_client.get("/admin/api/metrics")
        assert resp.status_code == 200

    def test_logfile(self, admin_client):
        resp = admin_client.get("/admin/api/logfile")
        assert resp.status_code == 200
        data = resp.json()
        assert "available" in data
        assert "records" in data
        assert "total" in data

    def test_logfile_clear(self, admin_client, log_handler):
        import logging

        logger = logging.getLogger("test.api.clear")
        logger.addHandler(log_handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("test entry")
            resp = admin_client.get("/admin/api/logfile")
            assert resp.json()["total"] >= 1

            resp = admin_client.post("/admin/api/logfile/clear")
            assert resp.status_code == 200
            assert resp.json()["cleared"] is True

            resp = admin_client.get("/admin/api/logfile")
            assert resp.json()["total"] == 0
        finally:
            logger.removeHandler(log_handler)

    def test_spa_routing(self, admin_client):
        resp = admin_client.get("/admin/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert '<base href="/admin/">' in resp.text

    def test_spa_no_trailing_slash(self, admin_client):
        resp = admin_client.get("/admin")
        assert resp.status_code == 200
        assert '<base href="/admin/">' in resp.text

    def test_spa_shell_is_no_cache(self, admin_client):
        # The SPA shell must revalidate so version-stamped asset URLs are
        # picked up after an upgrade (a heuristically cached index.html would
        # otherwise keep referencing the previous version's assets).
        resp = admin_client.get("/admin/")
        assert resp.headers.get("cache-control") == "no-cache"

    def test_spa_stamps_asset_versions(self, admin_client):
        from pyfly import __version__

        resp = admin_client.get("/admin/")
        # Local static assets carry the framework version as a cache-busting query.
        assert f"?v={__version__}" in resp.text
