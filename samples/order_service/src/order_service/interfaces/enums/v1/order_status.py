# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Order status state machine."""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    """Order lifecycle states.

    Mirrors :code:`FireflyFramework.Samples.OrdersService.Interfaces.Enums.V1.OrderStatus`
    in the .NET sample.
    """

    PLACED = "placed"
    INVENTORY_RESERVED = "inventory_reserved"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
