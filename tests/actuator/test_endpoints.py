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
"""Tests for actuator beans, env, info endpoints and create_app wiring."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
from pyfly.actuator.endpoints import BeansEndpoint, EnvEndpoint, HealthEndpoint, InfoEndpoint
from pyfly.actuator.health import HealthAggregator, HealthStatus
from pyfly.actuator.registry import ActuatorRegistry
from pyfly.container.stereotypes import component, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@component
class DummyComponent:
    pass


@service
class DummyService:
    pass


class SimpleHealthIndicator:
    """A HealthIndicator bean for wiring tests."""

    async def health(self) -> HealthStatus:
        return HealthStatus(status="UP", details={"test": True})


def _make_test_routes(context, agg=None):
    """Build actuator routes via the new registry API."""
    if agg is None:
        agg = HealthAggregator()
    registry = ActuatorRegistry()
    registry.register(HealthEndpoint(agg))
    registry.register(BeansEndpoint(context))
    registry.register(EnvEndpoint(context))
    registry.register(InfoEndpoint(context))
    return make_starlette_actuator_routes(registry)


# ---------------------------------------------------------------------------
# /actuator/beans
# ---------------------------------------------------------------------------


class TestBeansEndpoint:
    @pytest.mark.asyncio
    async def test_lists_registered_beans(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(DummyComponent)
        ctx.register_bean(DummyService)
        await ctx.start()

        routes = _make_test_routes(ctx)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/actuator/beans")
        assert resp.status_code == 200
        data = resp.json()
        # Spring Boot contexts envelope.
        beans = data["contexts"]["application"]["beans"]
        assert "DummyComponent" in beans
        assert "DummyService" in beans

    @pytest.mark.asyncio
    async def test_bean_details_include_type_scope_stereotype(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(DummyService)
        await ctx.start()

        routes = _make_test_routes(ctx)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/actuator/beans")
        svc_info = resp.json()["contexts"]["application"]["beans"]["DummyService"]
        assert "type" in svc_info
        assert svc_info["scope"] == "singleton"  # Spring uses lowercase scope
        assert svc_info["stereotype"] == "service"
        assert "dependencies" in svc_info
        assert "aliases" in svc_info


# ---------------------------------------------------------------------------
# /actuator/env
# ---------------------------------------------------------------------------


class TestEnvEndpoint:
    @pytest.mark.asyncio
    async def test_shows_active_profiles(self):
        cfg = Config({"pyfly": {"profiles": {"active": "dev,test"}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()

        routes = _make_test_routes(ctx)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/actuator/env")
        assert resp.status_code == 200
        data = resp.json()
        assert "activeProfiles" in data
        assert "dev" in data["activeProfiles"]
        assert "test" in data["activeProfiles"]


# ---------------------------------------------------------------------------
# /actuator/info
# ---------------------------------------------------------------------------


class TestInfoEndpoint:
    @pytest.mark.asyncio
    async def test_returns_app_info(self):
        cfg = Config({"pyfly": {"app": {"name": "myapp", "version": "1.0.0", "description": "A test app"}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()

        routes = _make_test_routes(ctx)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/actuator/info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["app"]["name"] == "myapp"
        assert data["app"]["version"] == "1.0.0"
        assert data["app"]["description"] == "A test app"


# ---------------------------------------------------------------------------
# /actuator index
# ---------------------------------------------------------------------------


class TestActuatorIndex:
    @pytest.mark.asyncio
    async def test_index_lists_enabled_endpoints(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()

        routes = _make_test_routes(ctx)
        app = Starlette(routes=routes)
        client = TestClient(app)

        resp = client.get("/actuator")
        assert resp.status_code == 200
        links = resp.json()["_links"]
        assert "self" in links
        assert "health" in links
        assert "beans" in links
        assert links["health"]["href"] == "/actuator/health"


# ---------------------------------------------------------------------------
# create_app(..., actuator_enabled=True)
# ---------------------------------------------------------------------------


class TestCreateAppActuatorWiring:
    @pytest.mark.asyncio
    async def test_actuator_endpoints_present_when_enabled(self):
        # Expose all endpoints (Spring-exact default exposes only health + info).
        cfg = Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=True)
        client = TestClient(app)

        # /actuator/health should be present
        resp = client.get("/actuator/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

        # /actuator/beans should be present (context was provided)
        resp = client.get("/actuator/beans")
        assert resp.status_code == 200
        assert "beans" in resp.json()["contexts"]["application"]

        # /actuator/env should be present
        resp = client.get("/actuator/env")
        assert resp.status_code == 200

        # /actuator/info should be present
        resp = client.get("/actuator/info")
        assert resp.status_code == 200

        # /actuator index should be present
        resp = client.get("/actuator")
        assert resp.status_code == 200
        assert "_links" in resp.json()

    @pytest.mark.asyncio
    async def test_actuator_not_present_when_disabled(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/actuator/health")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_health_indicators_auto_discovered(self):
        ctx = ApplicationContext(Config({}))
        # Register a HealthIndicator bean manually
        ctx.register_bean(SimpleHealthIndicator)
        ctx.container.bind(SimpleHealthIndicator, SimpleHealthIndicator)
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=True)
        client = TestClient(app)

        resp = client.get("/actuator/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "UP"
        # The indicator should be auto-discovered
        assert "SimpleHealthIndicator" in data.get("components", {})
