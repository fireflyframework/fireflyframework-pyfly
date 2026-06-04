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
"""Tests for Spring Boot-style actuator web exposure + reachability via create_app."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pyfly.actuator.exposure import base_path, is_web_exposed, web_exposure
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


class TestExposureModel:
    def test_default_exposes_only_health_and_info(self):
        include, exclude = web_exposure(Config({}))
        assert include == {"health", "info"}
        assert is_web_exposed("health", include, exclude)
        assert is_web_exposed("info", include, exclude)
        assert not is_web_exposed("metrics", include, exclude)
        assert not is_web_exposed("env", include, exclude)

    def test_wildcard_exposes_everything_except_excluded(self):
        cfg = Config(
            {"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*", "exclude": "env"}}}}}}
        )
        include, exclude = web_exposure(cfg)
        assert is_web_exposed("metrics", include, exclude)
        assert is_web_exposed("beans", include, exclude)
        assert not is_web_exposed("env", include, exclude)  # excluded wins

    def test_csv_include(self):
        cfg = Config(
            {"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "health,metrics,prometheus"}}}}}}
        )
        include, exclude = web_exposure(cfg)
        assert is_web_exposed("metrics", include, exclude)
        assert is_web_exposed("prometheus", include, exclude)
        assert not is_web_exposed("beans", include, exclude)

    def test_base_path_default_and_override(self):
        assert base_path(Config({})) == "/actuator"
        cfg = Config({"pyfly": {"management": {"endpoints": {"web": {"base-path": "/manage"}}}}})
        assert base_path(cfg) == "/manage"


class TestActuatorReachability:
    @pytest.mark.asyncio
    async def test_actuator_on_by_default_health_and_info_exposed(self):
        # No actuator_enabled kwarg, no management config -> Spring default ON,
        # only health + info web-exposed.
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(context=ctx)
        client = TestClient(app, raise_server_exceptions=False)

        assert client.get("/actuator/health").status_code == 200
        assert client.get("/actuator/info").status_code == 200
        # Not exposed by default:
        assert client.get("/actuator/metrics").status_code == 404
        assert client.get("/actuator/beans").status_code == 404

    @pytest.mark.asyncio
    async def test_prometheus_reachable_when_exposed(self):
        cfg = Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "prometheus"}}}}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()
        app = create_app(context=ctx)
        client = TestClient(app)

        resp = client.get("/actuator/prometheus")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_explicit_disable_overrides_config(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(context=ctx, actuator_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/actuator/health").status_code == 404

    @pytest.mark.asyncio
    async def test_config_can_disable_actuator(self):
        cfg = Config({"pyfly": {"management": {"enabled": False}}})
        ctx = ApplicationContext(cfg)
        await ctx.start()
        app = create_app(context=ctx)
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/actuator/health").status_code == 404

    @pytest.mark.asyncio
    async def test_custom_base_path(self):
        cfg = Config(
            {
                "pyfly": {
                    "management": {"endpoints": {"web": {"base-path": "/manage", "exposure": {"include": "health"}}}}
                }
            }
        )
        ctx = ApplicationContext(cfg)
        await ctx.start()
        app = create_app(context=ctx)
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/manage/health").status_code == 200
        assert client.get("/actuator/health").status_code == 404
