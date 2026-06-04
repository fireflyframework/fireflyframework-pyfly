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
"""Tests for user-defined ActuatorEndpoint auto-discovery."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from pyfly.container.stereotypes import component
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


@component
class GitInfoEndpoint:
    """Example user-defined actuator endpoint."""

    @property
    def endpoint_id(self) -> str:
        return "git"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        return {"branch": "main", "commit": "abc123"}


class TestCustomActuatorEndpoint:
    @pytest.mark.asyncio
    async def test_custom_endpoint_auto_discovered(self):
        cfg = Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}}})
        ctx = ApplicationContext(cfg)
        ctx.register_bean(GitInfoEndpoint)
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
        client = TestClient(app)

        resp = client.get("/actuator/git")
        assert resp.status_code == 200
        data = resp.json()
        assert data["branch"] == "main"
        assert data["commit"] == "abc123"

    @pytest.mark.asyncio
    async def test_custom_endpoint_in_index(self):
        cfg = Config({"pyfly": {"management": {"endpoints": {"web": {"exposure": {"include": "*"}}}}}})
        ctx = ApplicationContext(cfg)
        ctx.register_bean(GitInfoEndpoint)
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
        client = TestClient(app)

        resp = client.get("/actuator")
        links = resp.json()["_links"]
        assert "git" in links
        assert links["git"]["href"] == "/actuator/git"

    @pytest.mark.asyncio
    async def test_custom_endpoint_disabled_by_config(self):
        # git is exposed but explicitly disabled -> must 404.
        cfg = Config(
            {
                "pyfly": {
                    "management": {
                        "endpoints": {"web": {"exposure": {"include": "*"}}},
                        "endpoint": {"git": {"enabled": False}},
                    }
                }
            }
        )
        ctx = ApplicationContext(cfg)
        ctx.register_bean(GitInfoEndpoint)
        await ctx.start()

        app = create_app(context=ctx, actuator_enabled=True, docs_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/actuator/git")
        assert resp.status_code == 404
