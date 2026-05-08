# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""HTTP request DTO for ``POST /api/v1/orders``."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlaceOrderRequest(BaseModel):
    """Order-placement request payload.

    Mirrors :code:`FireflyFramework.Samples.OrdersService.Interfaces.Dtos.V1.PlaceOrderRequest`.
    """

    sku: str = Field(min_length=1, max_length=64, description="Stock-keeping unit")
    quantity: int = Field(gt=0, description="Number of units to order")
    unit_price: float = Field(gt=0, description="Price per unit in service currency")
