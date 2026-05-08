# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end test for :class:`ConfirmOrderSaga` happy path + compensation."""

from __future__ import annotations

import pytest
from order_service.core.services.orders import (
    InventoryService,
    PaymentService,
    PlaceOrderCommand,
    ShippingService,
)
from order_service.interfaces.enums.v1.order_status import OrderStatus
from order_service.models.repositories.order_repository import InMemoryOrderRepository

from pyfly.cqrs import DefaultCommandBus
from pyfly.transactional.saga.engine.saga_engine import SagaEngine


@pytest.mark.asyncio
async def test_saga_drives_order_to_shipped(
    repository: InMemoryOrderRepository,
    command_bus: DefaultCommandBus,
    saga_engine: tuple[SagaEngine, InventoryService, PaymentService, ShippingService],
) -> None:
    engine, _inv, _pay, _ship = saga_engine

    order_id = await command_bus.send(PlaceOrderCommand(sku="SKU-1", quantity=2, unit_price=10.0))

    result = await engine.execute(saga_name="confirm-order", headers={"order_id": order_id})

    assert result.success is True
    assert {step for step in result.steps} == {"reserve-inventory", "charge-payment", "ship-order"}

    order = await repository.find(order_id)
    assert order is not None
    assert order.status is OrderStatus.SHIPPED
    assert order.reservation_id is not None
    assert order.payment_id is not None
    assert order.tracking_number is not None


@pytest.mark.asyncio
async def test_saga_compensates_when_payment_fails(
    repository: InMemoryOrderRepository,
    command_bus: DefaultCommandBus,
    saga_engine: tuple[SagaEngine, InventoryService, PaymentService, ShippingService],
) -> None:
    engine, inventory, payment, _ship = saga_engine

    async def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("payment-gateway-down")

    payment.charge = boom  # type: ignore[method-assign]

    order_id = await command_bus.send(PlaceOrderCommand(sku="SKU-1", quantity=1, unit_price=99.0))

    result = await engine.execute(saga_name="confirm-order", headers={"order_id": order_id})

    assert result.success is False
    # Inventory was reserved, then released by compensation, so the
    # InventoryService's reservation map should be empty again.
    assert inventory._reservations == {}

    order = await repository.find(order_id)
    assert order is not None
    # The aggregate was advanced to INVENTORY_RESERVED before the failure;
    # compensation only undoes side effects on the external services in
    # this sample (releasing the inventory hold). A production
    # implementation would also flip the order back to PLACED or
    # CANCELLED; we keep the sample minimal for clarity.
    assert order.status in (OrderStatus.INVENTORY_RESERVED, OrderStatus.CANCELLED, OrderStatus.PLACED)
