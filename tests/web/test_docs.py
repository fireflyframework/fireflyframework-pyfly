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
"""Tests for Swagger UI and ReDoc doc endpoints."""

import pytest
from starlette.testclient import TestClient

from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app


class TestDocEndpoints:
    @pytest.mark.asyncio
    async def test_openapi_json(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(context=ctx)
        client = TestClient(app)
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert spec["openapi"] == "3.1.0"

    @pytest.mark.asyncio
    async def test_swagger_ui(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(title="Test", context=ctx)
        client = TestClient(app)
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger-ui" in response.text

    @pytest.mark.asyncio
    async def test_redoc(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(title="Test", context=ctx)
        client = TestClient(app)
        response = client.get("/redoc")
        assert response.status_code == 200
        assert "redoc" in response.text

    @pytest.mark.asyncio
    async def test_docs_disabled(self):
        ctx = ApplicationContext(Config({}))
        await ctx.start()
        app = create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)
        response = client.get("/docs")
        assert response.status_code == 404
