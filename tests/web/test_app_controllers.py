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
"""Tests for create_app() with ApplicationContext controller discovery."""

import pytest
from pydantic import BaseModel
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.mappings import get_mapping, request_mapping


class HealthResponse(BaseModel):
    status: str


@service
class HealthService:
    def check(self) -> str:
        return "ok"


@rest_controller
@request_mapping("/api/health")
class HealthController:
    def __init__(self, health_service: HealthService):
        self._svc = health_service

    @get_mapping("/")
    async def health(self) -> dict:
        return {"status": self._svc.check()}


class TestCreateAppWithContext:
    @pytest.mark.asyncio
    async def test_controller_routes_auto_discovered(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(HealthService)
        ctx.register_bean(HealthController)
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        response = client.get("/api/health/")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_docs_still_work(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()

        app = create_app(context=ctx, docs_enabled=True)
        client = TestClient(app)

        response = client.get("/docs")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_openapi_json_available(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()

        app = create_app(context=ctx)
        client = TestClient(app)

        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert spec["openapi"] == "3.1.0"
