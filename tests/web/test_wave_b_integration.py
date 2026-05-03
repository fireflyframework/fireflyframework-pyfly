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
"""Wave B integration test: controllers + request binding + auto-discovery."""

import pytest
from pydantic import BaseModel
from starlette.testclient import TestClient

from pyfly.container.stereotypes import repository, rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.context.lifecycle import post_construct
from pyfly.core.config import Config
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.exception_handler import exception_handler
from pyfly.web.mappings import delete_mapping, get_mapping, post_mapping, request_mapping
from pyfly.web.params import Body, PathVar, QueryParam

# --- Domain ---


class OrderNotFoundError(Exception):
    pass


class CreateOrderRequest(BaseModel):
    product: str
    quantity: int


class OrderResponse(BaseModel):
    id: str
    product: str
    quantity: int
    status: str


@repository
class OrderRepository:
    def __init__(self):
        self._store: dict[str, dict] = {}

    @post_construct
    async def seed(self):
        self._store["ord-001"] = {
            "id": "ord-001",
            "product": "Widget",
            "quantity": 5,
            "status": "created",
        }

    def find(self, order_id: str) -> dict:
        if order_id not in self._store:
            raise OrderNotFoundError(f"Order {order_id} not found")
        return self._store[order_id]

    def save(self, data: dict) -> dict:
        self._store[data["id"]] = data
        return data

    def delete(self, order_id: str) -> None:
        self._store.pop(order_id, None)

    def list_all(self, page: int, size: int) -> list[dict]:
        items = list(self._store.values())
        start = (page - 1) * size
        return items[start : start + size]


@service
class OrderService:
    def __init__(self, repo: OrderRepository):
        self._repo = repo

    def get_order(self, order_id: str) -> dict:
        return self._repo.find(order_id)

    def create_order(self, product: str, quantity: int) -> dict:
        order = {"id": "ord-new", "product": product, "quantity": quantity, "status": "created"}
        return self._repo.save(order)

    def list_orders(self, page: int, size: int) -> list[dict]:
        return self._repo.list_all(page, size)

    def delete_order(self, order_id: str) -> None:
        self._repo.delete(order_id)


@rest_controller
@request_mapping("/api/orders")
class OrderController:
    def __init__(self, order_service: OrderService):
        self._svc = order_service

    @get_mapping("/{order_id}")
    async def get_order(self, order_id: PathVar[str]) -> dict:
        return self._svc.get_order(order_id)

    @get_mapping("/")
    async def list_orders(self, page: QueryParam[int] = 1, size: QueryParam[int] = 20) -> list:
        return self._svc.list_orders(page, size)

    @post_mapping("/", status_code=201)
    async def create_order(self, body: Body[CreateOrderRequest]) -> dict:
        return self._svc.create_order(body.product, body.quantity)

    @delete_mapping("/{order_id}", status_code=204)
    async def delete_order(self, order_id: PathVar[str]) -> None:
        self._svc.delete_order(order_id)

    @exception_handler(OrderNotFoundError)
    async def handle_not_found(self, exc: OrderNotFoundError):
        return 404, {"error": {"code": "ORDER_NOT_FOUND", "message": str(exc)}}


# --- Integration test ---


class TestWaveBIntegration:
    @pytest.fixture
    async def client(self):
        ctx = ApplicationContext(Config({}))
        ctx.register_bean(OrderRepository)
        ctx.register_bean(OrderService)
        ctx.register_bean(OrderController)
        await ctx.start()

        app = create_app(title="Order Service", version="1.0.0", context=ctx)
        return TestClient(app)

    def test_get_seeded_order(self, client):
        response = client.get("/api/orders/ord-001")
        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "Widget"
        assert data["quantity"] == 5

    def test_list_orders(self, client):
        response = client.get("/api/orders/?page=1&size=10")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_create_order(self, client):
        response = client.post(
            "/api/orders/",
            json={"product": "Gadget", "quantity": 3},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["product"] == "Gadget"
        assert data["status"] == "created"

    def test_delete_order(self, client):
        response = client.delete("/api/orders/ord-001")
        assert response.status_code == 204

    def test_order_not_found(self, client):
        response = client.get("/api/orders/nonexistent")
        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "ORDER_NOT_FOUND"

    def test_openapi_available(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json()["openapi"] == "3.1.0"

    def test_swagger_ui_available(self, client):
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger-ui" in response.text
