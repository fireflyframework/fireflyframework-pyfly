# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""REST controller exposing the order management API."""

from __future__ import annotations

from order_service.core.services.orders.get_order_query import GetOrderQuery
from order_service.core.services.orders.place_order_command import PlaceOrderCommand
from order_service.interfaces.dtos.v1.order_dto import OrderDto
from order_service.interfaces.dtos.v1.place_order_request import PlaceOrderRequest
from pyfly.container import rest_controller
from pyfly.cqrs import CommandBus, QueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.transactional.saga import SagaEngine
from pyfly.web import Body, PathVar, Valid, get_mapping, post_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/orders")
class OrderController:
    """Mirrors :code:`FireflyFramework.Samples.OrdersService.Web.OrderController`."""

    def __init__(self, commands: CommandBus, queries: QueryBus, saga_engine: SagaEngine) -> None:
        self._commands = commands
        self._queries = queries
        self._saga_engine = saga_engine

    @post_mapping("", status_code=201)
    async def place_order(self, request: Valid[Body[PlaceOrderRequest]]) -> dict[str, str]:
        order_id = await self._commands.send(
            PlaceOrderCommand(sku=request.sku, quantity=request.quantity, unit_price=request.unit_price)
        )
        return {"order_id": order_id}

    @get_mapping("/{order_id}")
    async def get_order(self, order_id: PathVar[str]) -> OrderDto:
        result = await self._queries.query(GetOrderQuery(order_id=order_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Order {order_id!r} not found",
                code="ORDER_NOT_FOUND",
                context={"order_id": order_id},
            )
        return result

    @post_mapping("/{order_id}/confirm", status_code=202)
    async def confirm_order(self, order_id: PathVar[str]) -> dict[str, str]:
        result = await self._saga_engine.execute(
            saga_name="confirm-order",
            headers={"order_id": order_id},
        )
        return {
            "order_id": order_id,
            "saga_correlation_id": result.correlation_id,
            "status": "completed" if result.success else "failed",
        }
