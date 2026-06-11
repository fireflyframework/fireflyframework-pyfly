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
"""End-to-end integration test exercising multiple PyFly modules together.

Simulates a mini order-service that uses:
- DI Container for dependency injection
- CQRS for command/query routing
- EDA for event publishing
- Cache for result caching
- Security for authorization
- Validation for input validation
- Observability for metrics
- Web layer for HTTP error handling
"""

from dataclasses import dataclass

import pytest
from pydantic import BaseModel
from starlette.routing import Route
from starlette.testclient import TestClient

from pyfly.cache import cache
from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.container.stereotypes import rest_controller, service
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.cqrs import (
    Command,
    CommandHandler,
    DefaultCommandBus,
    DefaultQueryBus,
    HandlerRegistry,
    Query,
    QueryHandler,
    command_handler,
    query_handler,
)
from pyfly.eda import EventEnvelope
from pyfly.eda.adapters.memory import InMemoryEventBus
from pyfly.kernel.exceptions import ResourceNotFoundException, ValidationException
from pyfly.observability.metrics import MetricsRegistry, counted
from pyfly.security import SecurityContext, secure
from pyfly.testing import assert_event_published, create_test_container
from pyfly.validation import validate_model
from pyfly.web.adapters.starlette import create_app
from pyfly.web.mappings import get_mapping, post_mapping, request_mapping
from pyfly.web.params import Body, PathVar

# --- Domain models ---


class CreateOrderRequest(BaseModel):
    product: str
    quantity: int
    customer_id: str


@dataclass(frozen=True)
class CreateOrderCommand(Command[dict]):
    product: str
    quantity: int
    customer_id: str


@dataclass(frozen=True)
class GetOrderQuery(Query[dict]):
    order_id: str


# --- Handlers ---


@command_handler
class CreateOrderHandler(CommandHandler[CreateOrderCommand, dict]):
    async def do_handle(self, command: CreateOrderCommand) -> dict:
        return {
            "id": "ord-001",
            "product": command.product,
            "quantity": command.quantity,
            "customer_id": command.customer_id,
            "status": "created",
        }


@query_handler
class GetOrderHandler(QueryHandler[GetOrderQuery, dict]):
    async def do_handle(self, query: GetOrderQuery) -> dict:
        if query.order_id == "ord-001":
            return {"id": "ord-001", "product": "Widget", "status": "created"}
        raise ResourceNotFoundException(
            f"Order {query.order_id} not found",
            code="ORDER_NOT_FOUND",
        )


# --- Controller integration models ---


class ItemRequest(BaseModel):
    name: str
    price: float


@service
class IntegrationItemService:
    def get(self, item_id: str) -> dict:
        return {"id": item_id, "name": "Widget", "price": 9.99}

    def create(self, name: str, price: float) -> dict:
        return {"id": "new-1", "name": name, "price": price}


# --- Integration test ---


class TestEndToEndOrderService:
    @pytest.mark.asyncio
    async def test_full_order_flow(self):
        """Simulate creating an order through the full stack."""
        # 1. Set up infrastructure
        create_test_container()
        event_bus = InMemoryEventBus()
        cache_backend = InMemoryCache()
        metrics = MetricsRegistry()
        registry = HandlerRegistry()
        published_events: list[EventEnvelope] = []

        async def capture_events(event: EventEnvelope) -> None:
            published_events.append(event)

        event_bus.subscribe("order.*", capture_events)

        # 2. Register handlers
        registry.register_command_handler(CreateOrderHandler())
        registry.register_query_handler(GetOrderHandler())
        command_bus = DefaultCommandBus(registry=registry)
        query_bus = DefaultQueryBus(registry=registry)

        # 3. Validate input
        request_data = {"product": "Widget", "quantity": 5, "customer_id": "cust-001"}
        validated = validate_model(CreateOrderRequest, request_data)

        # 4. Security check
        ctx = SecurityContext(user_id="user-1", roles=["USER"], permissions=["order:create"])
        assert ctx.is_authenticated
        assert ctx.has_permission("order:create")

        # 5. Send command through CQRS bus
        command = CreateOrderCommand(
            product=validated.product,
            quantity=validated.quantity,
            customer_id=validated.customer_id,
        )
        order = await command_bus.send(command)
        assert order["id"] == "ord-001"
        assert order["status"] == "created"

        # 6. Publish event
        await event_bus.publish("order-events", "order.created", order)
        assert_event_published(published_events, "order.created", payload_contains={"id": "ord-001"})

        # 7. Query with caching
        @cache(backend=cache_backend, key="order:{order_id}")
        async def get_cached_order(order_id: str) -> dict:
            return await query_bus.query(GetOrderQuery(order_id=order_id))

        result1 = await get_cached_order("ord-001")
        result2 = await get_cached_order("ord-001")  # From cache
        assert result1 == result2
        assert result1["product"] == "Widget"

        # 8. Metrics — Micrometer @Counted style (counter "orders_created" -> _total)
        @counted(metrics, "orders.created", "Total orders created")
        async def tracked_create(data: dict) -> dict:
            return data

        await tracked_create(order)
        counter = metrics.counter("orders_created", "Total orders created")
        child = counter.labels(method="tracked_create", result="success", exception="none", **{"class": ""})
        assert child._value.get() == 1.0

    @pytest.mark.asyncio
    async def test_validation_rejects_bad_input(self):
        """Validate that bad input is caught before reaching the handler."""
        with pytest.raises(ValidationException) as exc_info:
            validate_model(CreateOrderRequest, {"product": "Widget"})
        assert exc_info.value.code == "VALIDATION_ERROR"

    @pytest.mark.asyncio
    async def test_security_blocks_unauthorized(self):
        """Verify that the @secure decorator blocks unauthorized access."""

        @secure(roles=["ADMIN"])
        async def admin_endpoint(security_context: SecurityContext) -> str:
            return "admin data"

        user_ctx = SecurityContext(user_id="user-1", roles=["USER"])
        with pytest.raises(Exception, match="Insufficient roles"):
            await admin_endpoint(security_context=user_ctx)

    @pytest.mark.asyncio
    async def test_not_found_propagates(self):
        """Verify ResourceNotFoundException propagates from query handler (wrapped by bus)."""
        from pyfly.cqrs.exceptions import QueryProcessingException

        registry = HandlerRegistry()
        registry.register_query_handler(GetOrderHandler())
        query_bus = DefaultQueryBus(registry=registry)

        with pytest.raises(QueryProcessingException) as exc_info:
            await query_bus.query(GetOrderQuery(order_id="nonexistent"))
        assert isinstance(exc_info.value.cause, ResourceNotFoundException)
        assert "not found" in str(exc_info.value.cause)

    def test_web_error_handling(self):
        """Verify that framework exceptions are properly mapped to HTTP responses."""

        async def get_order(request):
            raise ResourceNotFoundException("Order not found", code="ORDER_NOT_FOUND")

        app = create_app(title="test", extra_routes=[Route("/order/{order_id}", get_order)])

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/order/123")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "ORDER_NOT_FOUND"


class TestOpenAPIIntegration:
    @pytest.mark.asyncio
    async def test_controller_with_error_handling(self):
        """Verify controllers work alongside error handling."""

        @rest_controller
        @request_mapping("/api/items")
        class IntegrationItemCtrl:
            def __init__(self, svc: IntegrationItemService):
                self._svc = svc

            @get_mapping("/{item_id}")
            async def get_item(self, item_id: PathVar[str]) -> dict:
                return self._svc.get(item_id)

            @post_mapping("/", status_code=201)
            async def create_item(self, body: Body[ItemRequest]) -> dict:
                return self._svc.create(body.name, body.price)

        ctx = ApplicationContext(Config({}))
        ctx.register_bean(IntegrationItemService)
        ctx.register_bean(IntegrationItemCtrl)
        await ctx.start()

        app = create_app(title="Order Service", version="1.0.0", context=ctx)
        client = TestClient(app, raise_server_exceptions=False)

        # OpenAPI spec is served
        spec_resp = client.get("/openapi.json")
        assert spec_resp.status_code == 200
        assert spec_resp.json()["openapi"] == "3.1.0"

        # Swagger UI and ReDoc are served
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200

        # Routes actually work
        resp = client.get("/api/items/ord-001")
        assert resp.status_code == 200
        assert resp.json()["id"] == "ord-001"
