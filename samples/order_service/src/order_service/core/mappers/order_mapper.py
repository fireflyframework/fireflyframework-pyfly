# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Aggregate -> DTO mapping."""

from __future__ import annotations

from order_service.interfaces.dtos.v1.order_dto import OrderDto
from order_service.models.entities.v1.order_entity import Order


def order_to_dto(order: Order) -> OrderDto:
    """Project an :class:`Order` aggregate onto its public :class:`OrderDto`."""
    assert order.id is not None
    return OrderDto(
        id=order.id,
        sku=order.sku,
        quantity=order.quantity,
        unit_price=order.unit_price,
        total=order.total,
        status=order.status,
        created_at=order.created_at,
    )
