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
"""Tests for FastAPI web adapter."""

from importlib.util import find_spec

import pytest
from pydantic import BaseModel

from pyfly.container.stereotypes import controller_advice, rest_controller, service
from pyfly.web.exception_handler import exception_handler
from pyfly.web.mappings import delete_mapping, get_mapping, post_mapping, request_mapping
from pyfly.web.params import Body, PathVar, QueryParam
from pyfly.web.ports.outbound import WebServerPort

HAS_FASTAPI = find_spec("fastapi") is not None
pytestmark = pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")


# --- Test fixtures (module-level so type hints resolve correctly) ---


class CreateItemRequest(BaseModel):
    name: str


class ItemNotFoundError(Exception):
    pass


class ItemConflictError(Exception):
    """Raised by the controller but handled by a global @controller_advice bean."""


@controller_advice
class GlobalErrorAdvice:
    @exception_handler(ItemConflictError)
    async def handle_conflict(self, exc: ItemConflictError):
        return 409, {"error": str(exc), "scope": "global"}


@service
class ItemService:
    def find(self, item_id: str) -> dict:
        if item_id == "not-found":
            raise ItemNotFoundError(f"Item {item_id} not found")
        return {"id": item_id, "name": "Widget"}

    def create(self, name: str) -> dict:
        return {"id": "new-1", "name": name}

    def list_items(self, page: int, size: int) -> list[dict]:
        return [{"id": str(i), "name": f"Item {i}"} for i in range(size)]


@rest_controller
@request_mapping("/api/items")
class ItemController:
    def __init__(self, item_service: ItemService):
        self._svc = item_service

    @get_mapping("/{item_id}")
    async def get_item(self, item_id: PathVar[str]) -> dict:
        return self._svc.find(item_id)

    @get_mapping("/")
    async def list_items(self, page: QueryParam[int] = 1, size: QueryParam[int] = 10) -> list:
        return self._svc.list_items(page, size)

    @post_mapping("/", status_code=201)
    async def create_item(self, body: Body[CreateItemRequest]) -> dict:
        return self._svc.create(body.name)

    @delete_mapping("/{item_id}", status_code=204)
    async def delete_item(self, item_id: PathVar[str]) -> None:
        pass

    @get_mapping("/{item_id}/conflict")
    async def conflicting_item(self, item_id: PathVar[str]) -> dict:
        raise ItemConflictError(f"Item {item_id} already exists")

    @exception_handler(ItemNotFoundError)
    async def handle_not_found(self, exc: ItemNotFoundError):
        return 404, {"error": str(exc)}


class TestFastAPIWebAdapter:
    def test_is_web_server_port(self):
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        adapter = FastAPIWebAdapter()
        assert isinstance(adapter, WebServerPort)

    def test_create_app_returns_fastapi_instance(self):
        from fastapi import FastAPI

        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        adapter = FastAPIWebAdapter()
        app = adapter.create_app()
        assert isinstance(app, FastAPI)

    def test_create_app_with_title_and_version(self):
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(title="TestApp", version="2.0.0")
        assert app.title == "TestApp"
        assert app.version == "2.0.0"

    def test_create_app_docs_disabled(self):
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(docs_enabled=False)
        assert app.docs_url is None
        assert app.redoc_url is None
        assert app.openapi_url is None

    def test_create_app_docs_enabled_by_default(self):
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        adapter = FastAPIWebAdapter()
        app = adapter.create_app()
        assert app.docs_url == "/docs"
        assert app.redoc_url == "/redoc"
        assert app.openapi_url == "/openapi.json"


class TestFastAPIControllerRegistration:
    @pytest.mark.asyncio
    async def test_register_controllers_and_dispatch(self):
        """Controllers registered via FastAPIControllerRegistrar respond correctly."""
        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(context=ctx, docs_enabled=False)

        client = TestClient(app)

        # GET with path variable
        response = client.get("/api/items/abc-123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abc-123"
        assert data["name"] == "Widget"

        # GET with query params
        response = client.get("/api/items/?page=2&size=3")
        assert response.status_code == 200
        assert len(response.json()) == 3

        # POST with JSON body
        response = client.post("/api/items/", json={"name": "New Item"})
        assert response.status_code == 201
        assert response.json()["name"] == "New Item"

    @pytest.mark.asyncio
    async def test_delete_returns_204(self):
        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(context=ctx, docs_enabled=False)
        client = TestClient(app)

        response = client.delete("/api/items/abc-123")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_exception_handler_on_controller(self):
        """Per-controller @exception_handler methods are invoked."""
        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(context=ctx, docs_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/items/not-found")
        assert response.status_code == 404
        assert "not found" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_global_controller_advice_handler(self):
        """A @controller_advice global handler converts a controller exception."""
        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        ctx.register_bean(GlobalErrorAdvice)
        await ctx.start()

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(context=ctx, docs_enabled=False)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/items/abc/conflict")
        assert response.status_code == 409
        body = response.json()
        assert body["scope"] == "global"
        assert "already exists" in body["error"]


class TestFastAPIOpenAPIGeneration:
    @pytest.mark.asyncio
    async def test_openapi_json_includes_controller_route(self):
        """The generated /openapi.json includes auto-discovered controller paths."""
        from starlette.testclient import TestClient

        from pyfly.context.application_context import ApplicationContext
        from pyfly.core.config import Config
        from pyfly.web.adapters.fastapi.adapter import FastAPIWebAdapter

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        adapter = FastAPIWebAdapter()
        app = adapter.create_app(context=ctx, docs_enabled=True)
        client = TestClient(app)

        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert spec["openapi"] == "3.1.0"
        # Controller routes appear in the generated spec even though FastAPI's
        # own introspection registers them with include_in_schema=False.
        assert "/api/items/{item_id}" in spec["paths"]
        get_op = spec["paths"]["/api/items/{item_id}"]["get"]
        # These fields come from PyFly's OpenAPIGenerator reading the handler's
        # binding metadata — FastAPI's native introspection of a `PathVar[str]`
        # handler cannot produce them (it mangles operationId and omits the path
        # parameter), so this asserts the generated spec is actually in use.
        assert get_op["operationId"] == "get_item"
        path_params = [p for p in get_op.get("parameters", []) if p.get("in") == "path"]
        assert any(p["name"] == "item_id" for p in path_params)
