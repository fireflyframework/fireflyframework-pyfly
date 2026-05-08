# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP response DTO for ``GET /api/v1/orders/{id}``."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from order_service.interfaces.enums.v1.order_status import OrderStatus


class OrderDto(BaseModel):
    """Full order representation returned to clients.

    Mirrors :code:`FireflyFramework.Samples.OrdersService.Interfaces.Dtos.V1.OrderDto`.
    """

    id: str
    sku: str
    quantity: int
    unit_price: float
    total: float
    status: OrderStatus
    created_at: datetime
