# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``GetOrderQuery`` — read-side intent for fetching a single order."""

from __future__ import annotations

from dataclasses import dataclass

from order_service.interfaces.dtos.v1.order_dto import OrderDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetOrderQuery(Query[OrderDto | None]):
    """Look up an order by its identifier."""

    order_id: str
