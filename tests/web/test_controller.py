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
"""Tests for controller discovery and route building."""

import pytest
from pydantic import BaseModel
from starlette.testclient import TestClient

from pyfly.container.stereotypes import rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.controller import ControllerRegistrar
from pyfly.web.exception_handler import exception_handler
from pyfly.web.mappings import delete_mapping, get_mapping, post_mapping, request_mapping
from pyfly.web.params import Body, PathVar, QueryParam


class ItemResponse(BaseModel):
    id: str
    name: str


class CreateItemRequest(BaseModel):
    name: str


class ItemNotFoundError(Exception):
    pass


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

    @exception_handler(ItemNotFoundError)
    async def handle_not_found(self, exc: ItemNotFoundError):
        return 404, {"error": str(exc)}


class TestControllerRegistrar:
    @pytest.mark.asyncio
    async def test_collect_routes(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)
        assert len(routes) >= 4  # get, list, create, delete

    @pytest.mark.asyncio
    async def test_route_paths(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)
        paths = {r.path for r in routes}
        assert "/api/items/{item_id}" in paths
        assert "/api/items/" in paths

    def test_collect_routes_no_eager_bean_resolution(self):
        """Routes are created from class metadata — no get_bean() calls during collection."""
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        # NOTE: ctx.start() is NOT called — beans can't be resolved yet

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)
        assert len(routes) >= 4
        paths = {r.path for r in routes}
        assert "/api/items/{item_id}" in paths
        assert "/api/items/" in paths


class TestControllerIntegration:
    @pytest.mark.asyncio
    async def test_get_item(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)

        from starlette.applications import Starlette

        app = Starlette(routes=routes)
        client = TestClient(app)

        response = client.get("/api/items/abc-123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abc-123"
        assert data["name"] == "Widget"

    @pytest.mark.asyncio
    async def test_list_items_with_query(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)

        from starlette.applications import Starlette

        app = Starlette(routes=routes)
        client = TestClient(app)

        response = client.get("/api/items/?page=2&size=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 5

    @pytest.mark.asyncio
    async def test_create_item_with_body(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)

        from starlette.applications import Starlette

        app = Starlette(routes=routes)
        client = TestClient(app)

        response = client.post("/api/items/", json={"name": "New Item"})
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Item"

    @pytest.mark.asyncio
    async def test_delete_returns_204(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)

        from starlette.applications import Starlette

        app = Starlette(routes=routes)
        client = TestClient(app)

        response = client.delete("/api/items/abc-123")
        assert response.status_code == 204

    @pytest.mark.asyncio
    async def test_exception_handler_catches(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(ItemService)
        ctx.register_bean(ItemController)
        await ctx.start()

        registrar = ControllerRegistrar()
        routes = registrar.collect_routes(ctx)

        from starlette.applications import Starlette

        app = Starlette(routes=routes)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/api/items/not-found")
        assert response.status_code == 404
        assert "not found" in response.json()["error"]
