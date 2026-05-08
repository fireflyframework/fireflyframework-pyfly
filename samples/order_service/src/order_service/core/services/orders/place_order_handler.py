# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Handler for :class:`PlaceOrderCommand`.

Creates the :class:`Order` aggregate, persists it, and drains its
``pending_events`` so the application service can publish them to the
event bus.
"""

from __future__ import annotations

import logging

from order_service.core.services.orders.place_order_command import PlaceOrderCommand
from order_service.models.entities.v1.order_entity import Order
from order_service.models.repositories.order_repository import OrderRepository
from pyfly.cqrs import CommandHandler, command_handler

logger = logging.getLogger(__name__)


@command_handler
class PlaceOrderHandler(CommandHandler[PlaceOrderCommand, str]):
    """Place an order in ``OrderStatus.PLACED``."""

    def __init__(self, repository: OrderRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, command: PlaceOrderCommand) -> str:  # type: ignore[override]
        order_id = await self._repository.next_id()
        order = Order.place(
            order_id=order_id,
            sku=command.sku,
            quantity=command.quantity,
            unit_price=command.unit_price,
        )
        await self._repository.add(order)

        for event in order.clear_events():
            logger.info(
                "domain_event",
                extra={"event_type": event.event_type, "event_id": event.event_id},
            )
        return order_id
