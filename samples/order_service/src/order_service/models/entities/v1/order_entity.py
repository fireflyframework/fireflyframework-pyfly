# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Domain :class:`Order` aggregate.

This is a real DDD aggregate root built on
:class:`pyfly.domain.AggregateRoot`. State changes happen through
intent-revealing methods (``place``, ``reserve_inventory``, ``mark_paid``,
``ship``, ``cancel``) which protect the invariants and raise
:class:`pyfly.domain.DomainEvent` instances. The repository drains the
events with ``clear_events()`` after persisting and the application
service publishes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from order_service.interfaces.enums.v1.order_status import OrderStatus
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent

# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str = ""
    sku: str = ""
    quantity: int = 0
    unit_price: float = 0.0
    total: float = 0.0


@dataclass(frozen=True)
class OrderInventoryReserved(DomainEvent):
    order_id: str = ""
    reservation_id: str = ""


@dataclass(frozen=True)
class OrderPaid(DomainEvent):
    order_id: str = ""
    payment_id: str = ""


@dataclass(frozen=True)
class OrderShipped(DomainEvent):
    order_id: str = ""
    tracking_number: str = ""


@dataclass(frozen=True)
class OrderCancelled(DomainEvent):
    order_id: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Aggregate root
# ---------------------------------------------------------------------------


class Order(AggregateRoot[str]):
    """Order aggregate root."""

    __slots__ = (
        "sku",
        "quantity",
        "unit_price",
        "total",
        "status",
        "reservation_id",
        "payment_id",
        "tracking_number",
        "created_at",
    )

    def __init__(
        self,
        id: str,
        sku: str,
        quantity: int,
        unit_price: float,
        status: OrderStatus = OrderStatus.PLACED,
        created_at: datetime | None = None,
    ) -> None:
        super().__init__(id)
        self.sku = sku
        self.quantity = quantity
        self.unit_price = unit_price
        self.total = round(quantity * unit_price, 2)
        self.status = status
        self.reservation_id: str | None = None
        self.payment_id: str | None = None
        self.tracking_number: str | None = None
        self.created_at = created_at or datetime.now(UTC)

    # --- factory ---------------------------------------------------------

    @classmethod
    def place(cls, order_id: str, sku: str, quantity: int, unit_price: float) -> Order:
        """Place a new order; raises :class:`OrderPlaced`."""
        if quantity <= 0:
            raise BusinessRuleViolation("order-quantity-positive", "quantity must be > 0")
        if unit_price <= 0:
            raise BusinessRuleViolation("order-unit-price-positive", "unit_price must be > 0")
        if not sku.strip():
            raise BusinessRuleViolation("order-sku-required", "sku is required")
        order = cls(id=order_id, sku=sku, quantity=quantity, unit_price=unit_price)
        order.raise_event(
            OrderPlaced(
                order_id=order_id,
                sku=sku,
                quantity=quantity,
                unit_price=unit_price,
                total=order.total,
            )
        )
        return order

    # --- transitions -----------------------------------------------------

    def reserve_inventory(self, reservation_id: str) -> None:
        if self.status is not OrderStatus.PLACED:
            raise BusinessRuleViolation(
                "order-must-be-placed",
                f"cannot reserve inventory while status is {self.status.value}",
            )
        self.status = OrderStatus.INVENTORY_RESERVED
        self.reservation_id = reservation_id
        assert self.id is not None
        self.raise_event(OrderInventoryReserved(order_id=self.id, reservation_id=reservation_id))

    def mark_paid(self, payment_id: str) -> None:
        if self.status is not OrderStatus.INVENTORY_RESERVED:
            raise BusinessRuleViolation(
                "order-must-be-reserved",
                f"cannot charge while status is {self.status.value}",
            )
        self.status = OrderStatus.PAID
        self.payment_id = payment_id
        assert self.id is not None
        self.raise_event(OrderPaid(order_id=self.id, payment_id=payment_id))

    def ship(self, tracking_number: str) -> None:
        if self.status is not OrderStatus.PAID:
            raise BusinessRuleViolation(
                "order-must-be-paid",
                f"cannot ship while status is {self.status.value}",
            )
        self.status = OrderStatus.SHIPPED
        self.tracking_number = tracking_number
        assert self.id is not None
        self.raise_event(OrderShipped(order_id=self.id, tracking_number=tracking_number))

    def cancel(self, reason: str) -> None:
        if self.status is OrderStatus.SHIPPED:
            raise BusinessRuleViolation(
                "order-cannot-cancel-after-ship",
                "shipped orders cannot be cancelled",
            )
        if self.status is OrderStatus.CANCELLED:
            return
        self.status = OrderStatus.CANCELLED
        assert self.id is not None
        self.raise_event(OrderCancelled(order_id=self.id, reason=reason))
