# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the :class:`Order` aggregate root."""

from __future__ import annotations

import pytest
from order_service.interfaces.enums.v1.order_status import OrderStatus
from order_service.models.entities.v1.order_entity import (
    Order,
    OrderInventoryReserved,
    OrderPaid,
    OrderPlaced,
    OrderShipped,
)

from pyfly.domain import BusinessRuleViolation


def test_place_creates_aggregate_in_placed_status() -> None:
    order = Order.place("ord-1", "SKU-1", 2, 10.0)
    assert order.status is OrderStatus.PLACED
    assert order.total == 20.0
    [event] = order.pending_events()
    assert isinstance(event, OrderPlaced)
    assert event.order_id == "ord-1"


@pytest.mark.parametrize(
    ("sku", "qty", "price", "rule"),
    [
        ("", 1, 10.0, "order-sku-required"),
        ("SKU-1", 0, 10.0, "order-quantity-positive"),
        ("SKU-1", 1, 0.0, "order-unit-price-positive"),
    ],
)
def test_place_enforces_invariants(sku: str, qty: int, price: float, rule: str) -> None:
    with pytest.raises(BusinessRuleViolation) as exc:
        Order.place("ord-x", sku, qty, price)
    assert exc.value.rule == rule


def test_state_machine_happy_path() -> None:
    order = Order.place("ord-2", "SKU-1", 1, 50.0)
    order.clear_events()

    order.reserve_inventory("resv-1")
    assert order.status is OrderStatus.INVENTORY_RESERVED
    [event] = order.clear_events()
    assert isinstance(event, OrderInventoryReserved)

    order.mark_paid("pay-1")
    assert order.status is OrderStatus.PAID
    [event] = order.clear_events()
    assert isinstance(event, OrderPaid)

    order.ship("trk-1")
    assert order.status is OrderStatus.SHIPPED
    [event] = order.clear_events()
    assert isinstance(event, OrderShipped)


def test_cannot_skip_states() -> None:
    order = Order.place("ord-3", "SKU-1", 1, 50.0)
    with pytest.raises(BusinessRuleViolation) as exc:
        order.mark_paid("pay-1")
    assert exc.value.rule == "order-must-be-reserved"

    order.reserve_inventory("resv-1")
    with pytest.raises(BusinessRuleViolation) as exc:
        order.ship("trk-1")
    assert exc.value.rule == "order-must-be-paid"


def test_cannot_cancel_after_ship() -> None:
    order = Order.place("ord-4", "SKU-1", 1, 50.0)
    order.reserve_inventory("r")
    order.mark_paid("p")
    order.ship("t")
    with pytest.raises(BusinessRuleViolation):
        order.cancel("oops")


def test_cancel_is_idempotent() -> None:
    order = Order.place("ord-5", "SKU-1", 1, 50.0)
    order.cancel("changed-mind")
    events_before = len(order.pending_events())
    order.cancel("changed-mind-again")
    assert len(order.pending_events()) == events_before
