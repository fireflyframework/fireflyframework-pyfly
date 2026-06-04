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
"""Spring-shaped admin Environment/Configuration providers + /actuator/env (real Config)."""

from __future__ import annotations

import pytest

from pyfly.actuator.endpoints.env_endpoint import EnvEndpoint
from pyfly.admin.providers.config_provider import ConfigProvider
from pyfly.admin.providers.env_provider import EnvProvider
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config


def _ctx() -> ApplicationContext:
    cfg = Config(
        {
            "pyfly": {
                "app": {"name": "svc"},
                "web": {"port": 8080, "docs": {"enabled": True}},
                "security": {"jwt": {"secret": "supersecret"}},
                "profiles": {"active": "dev"},
            }
        }
    )
    return ApplicationContext(cfg)


class TestEnvProvider:
    @pytest.mark.asyncio
    async def test_property_sources_and_masking(self, monkeypatch):
        monkeypatch.setenv("PYFLY_WEB_PORT", "9090")
        ctx = _ctx()
        result = await EnvProvider(ctx).get_env()

        assert "dev" in result["activeProfiles"]
        # Ordered sources, systemEnvironment first.
        names = [s["name"] for s in result["propertySources"]]
        assert names[0] == "systemEnvironment"
        # env override visible in effective properties + coerced.
        assert result["properties"]["pyfly.web.port"] == 9090
        # secret masked everywhere.
        assert result["properties"]["pyfly.security.jwt.secret"] == "******"
        # origin attribution present.
        assert result["origins"]["pyfly.web.port"] == "systemEnvironment"

    @pytest.mark.asyncio
    async def test_properties_sorted(self):
        ctx = _ctx()
        result = await EnvProvider(ctx).get_env()
        keys = list(result["properties"].keys())
        assert keys == sorted(keys)


class TestConfigProvider:
    @pytest.mark.asyncio
    async def test_grouped_sorted_masked_attributed(self):
        ctx = _ctx()
        result = await ConfigProvider(ctx).get_config()
        groups = result["groups"]

        # Sorted group order.
        assert list(groups.keys()) == sorted(groups.keys())
        # Grouped by 2-segment prefix.
        assert "pyfly.web" in groups
        web = groups["pyfly.web"]
        # Deeply-nested keys flattened (no raw JSON blob).
        assert web["port"]["value"] == 8080
        assert web["docs.enabled"]["value"] is True
        # Secret masked + flagged.
        sec = groups["pyfly.security"]
        assert sec["jwt.secret"]["value"] == "******"
        assert sec["jwt.secret"]["sensitive"] is True


class TestActuatorEnvEndpoint:
    @pytest.mark.asyncio
    async def test_env_propertysources_shape(self):
        ctx = _ctx()
        ep = EnvEndpoint(ctx)
        data = await ep.handle()
        assert "activeProfiles" in data
        assert isinstance(data["propertySources"], list)
        # Masked secret in the source view too.
        flat = {k: v for s in data["propertySources"] for k, v in s["properties"].items()}
        assert flat["pyfly.security.jwt.secret"]["value"] == "******"

    @pytest.mark.asyncio
    async def test_env_selector_returns_single_property(self):
        ctx = _ctx()
        ep = EnvEndpoint(ctx)
        data = await ep.handle({"selector": "pyfly.web.port"})
        assert data["property"] is not None
        assert data["property"]["value"] == 8080
