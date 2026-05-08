# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Order repository — port + in-memory adapter.

The :class:`OrderRepository` protocol implements the
:class:`pyfly.domain.DomainRepository` contract. Replace the in-memory
adapter with a real SQLAlchemy/MongoDB-backed implementation when
deploying to production — your business logic stays unchanged.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol, runtime_checkable

from order_service.models.entities.v1.order_entity import Order
from pyfly.container import repository


@runtime_checkable
class OrderRepository(Protocol):
    async def add(self, order: Order) -> Order: ...
    async def find(self, id: str) -> Order | None: ...
    async def remove(self, order: Order) -> None: ...
    async def next_id(self) -> str: ...


@repository
class InMemoryOrderRepository:
    """Concurrent in-memory store keyed by order id."""

    def __init__(self) -> None:
        self._store: dict[str, Order] = {}
        self._lock = asyncio.Lock()

    async def add(self, order: Order) -> Order:
        async with self._lock:
            assert order.id is not None
            self._store[order.id] = order
            return order

    async def find(self, id: str) -> Order | None:
        async with self._lock:
            return self._store.get(id)

    async def remove(self, order: Order) -> None:
        async with self._lock:
            if order.id is not None:
                self._store.pop(order.id, None)

    async def next_id(self) -> str:
        return f"ord-{uuid.uuid4()}"
