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
"""Regression tests for admin wiring fixes.

#67 — server mode mounts the instance-registry routes from configured instances.
#68 — AdminClientRegistration self-registers via start()/stop() lifecycle.
#70 — the admin health aggregator is re-scanned after startup.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from pyfly.actuator.health import HealthStatus
from pyfly.admin.server.client_registration import AdminClientRegistration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


# ---------------------------------------------------------------------------
# #67 — server mode instance registry
# ---------------------------------------------------------------------------


class TestServerMode:
    def test_instances_route_mounted_and_discovered(self):
        ctx = ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "admin": {
                            "enabled": "true",
                            "server": {"enabled": "true", "instances": [{"name": "a", "url": "http://a"}]},
                        }
                    }
                }
            )
        )
        client = TestClient(create_app(context=ctx, actuator_enabled=False))
        resp = client.get("/admin/api/instances")
        assert resp.status_code == 200
        assert [i["name"] for i in resp.json()["instances"]] == ["a"]
        assert client.get("/admin/api/settings").json()["serverMode"] is True

    def test_no_server_mode_no_instances_route(self):
        ctx = ApplicationContext(Config({"pyfly": {"admin": {"enabled": "true"}}}))
        client = TestClient(create_app(context=ctx, actuator_enabled=False))
        assert client.get("/admin/api/settings").json()["serverMode"] is False
        # The instances route is not mounted, so the request falls through to the
        # SPA catch-all (HTML), not the JSON registry payload.
        resp = client.get("/admin/api/instances")
        assert "application/json" not in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# #68 — client self-registration lifecycle
# ---------------------------------------------------------------------------


class TestClientRegistration:
    def test_is_a_lifecycle_bean(self):
        reg = AdminClientRegistration("http://s", "app", "http://app")
        assert ApplicationContext._has_lifecycle_methods(reg) is True

    @pytest.mark.asyncio
    async def test_start_registers_when_auto(self):
        reg = AdminClientRegistration("http://s", "app", "http://app", auto_register=True)
        reg.register = AsyncMock(return_value=True)  # type: ignore[method-assign]
        reg.deregister = AsyncMock(return_value=True)  # type: ignore[method-assign]
        await reg.start()
        reg.register.assert_awaited_once()
        await reg.stop()
        reg.deregister.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_noop_when_not_auto(self):
        reg = AdminClientRegistration("http://s", "app", "http://app", auto_register=False)
        reg.register = AsyncMock()  # type: ignore[method-assign]
        await reg.start()
        reg.register.assert_not_awaited()


# ---------------------------------------------------------------------------
# #70 — admin health aggregator post-start rescan
# ---------------------------------------------------------------------------


class _Indicator:
    async def health(self) -> HealthStatus:
        return HealthStatus(status="UP")


class TestAdminHealthRescan:
    def test_post_start_indicator_reflected(self):
        ctx = ApplicationContext(Config({"pyfly": {"admin": {"enabled": "true"}}}))
        app = create_app(context=ctx, actuator_enabled=False)

        # Register an indicator AFTER create_app, mimicking a bean instantiated
        # during start(); the post-start rescan must fold it in (#70).
        ctx.container.register(_Indicator)
        ctx.container._registrations[_Indicator].instance = _Indicator()
        app.state.pyfly_install_dynamic_wiring()

        client = TestClient(app)
        components = client.get("/admin/api/health").json().get("components", {})
        assert "_Indicator" in components
        assert components["_Indicator"]["status"] == "UP"
