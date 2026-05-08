# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end tests for the CQRS place-order + get-order flow."""

from __future__ import annotations

import pytest
from order_service.core.services.orders.get_order_query import GetOrderQuery
from order_service.core.services.orders.place_order_command import PlaceOrderCommand
from order_service.interfaces.enums.v1.order_status import OrderStatus

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_place_order_returns_id_and_get_order_returns_dto(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    order_id = await command_bus.send(PlaceOrderCommand(sku="SKU-1", quantity=2, unit_price=15.0))
    assert isinstance(order_id, str) and order_id.startswith("ord-")

    dto = await query_bus.query(GetOrderQuery(order_id=order_id))
    assert dto is not None
    assert dto.id == order_id
    assert dto.sku == "SKU-1"
    assert dto.quantity == 2
    assert dto.unit_price == 15.0
    assert dto.total == 30.0
    assert dto.status is OrderStatus.PLACED


@pytest.mark.asyncio
async def test_get_order_returns_none_for_unknown_id(query_bus: DefaultQueryBus) -> None:
    assert await query_bus.query(GetOrderQuery(order_id="ord-does-not-exist")) is None


@pytest.mark.asyncio
async def test_validation_rejects_blank_sku(command_bus: DefaultCommandBus) -> None:
    from pyfly.cqrs.exceptions import CommandProcessingException

    with pytest.raises(CommandProcessingException):
        await command_bus.send(PlaceOrderCommand(sku="", quantity=1, unit_price=10.0))
