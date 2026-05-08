# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``ConfirmOrderSaga`` — drives an order from PLACED through to SHIPPED.

The saga coordinates three external services (inventory, payment,
shipping) and compensates each one if a later step fails. The
implementations here are **in-memory stubs** so the sample runs without
external infrastructure; in a real deployment they would be HTTP
clients to the respective microservices.

Flow::

    reserve_inventory --> charge_payment --> ship_order

Compensation::

    release_inventory <-- refund_payment <-- (none for shipping)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

from order_service.models.entities.v1.order_entity import Order
from order_service.models.repositories.order_repository import OrderRepository
from pyfly.container import service
from pyfly.transactional.saga import FromStep, saga, saga_step
from pyfly.transactional.saga.core.context import SagaContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub external services (replaced by HTTP clients in production)
# ---------------------------------------------------------------------------


@service
class InventoryService:
    """Pretends to reserve and release stock for a SKU."""

    def __init__(self) -> None:
        self._reservations: dict[str, str] = {}

    async def reserve(self, order_id: str, sku: str, quantity: int) -> str:
        await asyncio.sleep(0)  # simulate I/O
        reservation_id = f"resv-{uuid.uuid4()}"
        self._reservations[reservation_id] = order_id
        logger.info("inventory_reserved", extra={"order_id": order_id, "sku": sku, "qty": quantity})
        return reservation_id

    async def release(self, reservation_id: str) -> None:
        await asyncio.sleep(0)
        self._reservations.pop(reservation_id, None)
        logger.info("inventory_released", extra={"reservation_id": reservation_id})


@service
class PaymentService:
    """Pretends to charge and refund a payment."""

    def __init__(self) -> None:
        self._payments: dict[str, str] = {}

    async def charge(self, order_id: str, amount: float) -> str:
        await asyncio.sleep(0)
        payment_id = f"pay-{uuid.uuid4()}"
        self._payments[payment_id] = order_id
        logger.info("payment_charged", extra={"order_id": order_id, "amount": amount})
        return payment_id

    async def refund(self, payment_id: str) -> None:
        await asyncio.sleep(0)
        self._payments.pop(payment_id, None)
        logger.info("payment_refunded", extra={"payment_id": payment_id})


@service
class ShippingService:
    """Pretends to dispatch a shipment."""

    async def dispatch(self, order_id: str) -> str:
        await asyncio.sleep(0)
        tracking = f"trk-{uuid.uuid4()}"
        logger.info("order_dispatched", extra={"order_id": order_id, "tracking": tracking})
        return tracking


# ---------------------------------------------------------------------------
# Saga
# ---------------------------------------------------------------------------


@saga(name="confirm-order")
@service
class ConfirmOrderSaga:
    """Drive an order from PLACED to SHIPPED, with full compensation."""

    def __init__(
        self,
        repository: OrderRepository,
        inventory: InventoryService,
        payment: PaymentService,
        shipping: ShippingService,
    ) -> None:
        self._repository = repository
        self._inventory = inventory
        self._payment = payment
        self._shipping = shipping

    async def _load(self, ctx: SagaContext) -> Order:
        order_id = ctx.headers.get("order_id") or ctx.get_variable("order_id")
        if not order_id:
            raise ValueError("ConfirmOrderSaga requires 'order_id' in headers or variables")
        order = await self._repository.find(str(order_id))
        if order is None:
            raise LookupError(f"Order {order_id!r} not found")
        return order

    async def _save_and_drain(self, order: Order) -> None:
        await self._repository.add(order)
        for event in order.clear_events():
            logger.info(
                "domain_event",
                extra={"event_type": event.event_type, "event_id": event.event_id},
            )

    # --- step 1: reserve inventory ----------------------------------------

    @saga_step(id="reserve-inventory", retry=2, backoff_ms=200, compensate="release_inventory")
    async def reserve_inventory(self, ctx: SagaContext) -> str:
        order = await self._load(ctx)
        reservation_id = await self._inventory.reserve(
            order_id=str(order.id),
            sku=order.sku,
            quantity=order.quantity,
        )
        order.reserve_inventory(reservation_id)
        await self._save_and_drain(order)
        ctx.set_variable("reservation_id", reservation_id)
        return reservation_id

    async def release_inventory(self, ctx: SagaContext) -> None:
        reservation_id = ctx.get_variable("reservation_id")
        if reservation_id:
            await self._inventory.release(str(reservation_id))

    # --- step 2: charge payment -------------------------------------------

    @saga_step(
        id="charge-payment",
        depends_on=["reserve-inventory"],
        retry=3,
        backoff_ms=300,
        compensate="refund_payment",
    )
    async def charge_payment(
        self,
        ctx: SagaContext,
        reservation_id: Annotated[str, FromStep("reserve-inventory")],
    ) -> str:
        del reservation_id  # presence implies inventory step succeeded
        order = await self._load(ctx)
        payment_id = await self._payment.charge(order_id=str(order.id), amount=order.total)
        order.mark_paid(payment_id)
        await self._save_and_drain(order)
        ctx.set_variable("payment_id", payment_id)
        return payment_id

    async def refund_payment(self, ctx: SagaContext) -> None:
        payment_id = ctx.get_variable("payment_id")
        if payment_id:
            await self._payment.refund(str(payment_id))

    # --- step 3: ship -----------------------------------------------------

    @saga_step(id="ship-order", depends_on=["charge-payment"])
    async def ship_order(
        self,
        ctx: SagaContext,
        payment_id: Annotated[str, FromStep("charge-payment")],
    ) -> str:
        del payment_id
        order = await self._load(ctx)
        tracking = await self._shipping.dispatch(order_id=str(order.id))
        order.ship(tracking)
        await self._save_and_drain(order)
        ctx.set_variable("tracking_number", tracking)
        return tracking
