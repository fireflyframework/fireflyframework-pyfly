# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`GetOrderQuery`."""

from __future__ import annotations

from order_service.core.mappers.order_mapper import order_to_dto
from order_service.core.services.orders.get_order_query import GetOrderQuery
from order_service.interfaces.dtos.v1.order_dto import OrderDto
from order_service.models.repositories.order_repository import OrderRepository
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
class GetOrderHandler(QueryHandler[GetOrderQuery, OrderDto | None]):
    def __init__(self, repository: OrderRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetOrderQuery) -> OrderDto | None:  # type: ignore[override]
        order = await self._repository.find(query.order_id)
        return order_to_dto(order) if order is not None else None
