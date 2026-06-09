# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Item 2 — @publish_domain_event destination honoured by the command bus.

Guards against the historical bug where ``_try_publish_events`` always called
``publisher.publish(event)`` without a ``destination`` argument, so the
decorator metadata ``__pyfly_event_destination__`` was silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import command_handler
from pyfly.cqrs.event.decorators import publish_domain_event
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command

# ---------------------------------------------------------------------------
# Capturing publisher
# ---------------------------------------------------------------------------


class CapturingPublisher:
    """Records every (event, destination) pair passed to publish()."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str | None]] = []

    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        self.calls.append((event, destination))


# ---------------------------------------------------------------------------
# Domain event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderPlaced:
    order_id: str
    amount: float

    @property
    def event_type(self) -> str:
        return "order.placed"


# ---------------------------------------------------------------------------
# Commands and handlers
# ---------------------------------------------------------------------------


@dataclass
class PlaceOrderCommand(Command[str]):
    customer_id: str = ""
    amount: float = 0.0
    domain_events: list[Any] = field(default_factory=list)


@dataclass
class PlaceOrderNoDestCommand(Command[str]):
    customer_id: str = ""
    amount: float = 0.0
    domain_events: list[Any] = field(default_factory=list)


@publish_domain_event(destination="orders.events")
@command_handler
class PlaceOrderHandler(CommandHandler[PlaceOrderCommand, str]):
    async def do_handle(self, command: PlaceOrderCommand) -> str:
        oid = f"order-{command.customer_id}"
        command.domain_events.append(OrderPlaced(order_id=oid, amount=command.amount))
        return oid


@command_handler
class PlaceOrderNoDestHandler(CommandHandler[PlaceOrderNoDestCommand, str]):
    """Handler without @publish_domain_event — destination should be None."""

    async def do_handle(self, command: PlaceOrderNoDestCommand) -> str:
        oid = f"order-{command.customer_id}"
        command.domain_events.append(OrderPlaced(order_id=oid, amount=command.amount))
        return oid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPublishDomainEventDestination:
    @pytest.fixture(autouse=True)
    def _clear_correlation(self) -> None:
        CorrelationContext.clear()

    async def test_decorator_destination_forwarded_to_publisher(self) -> None:
        publisher = CapturingPublisher()
        registry = HandlerRegistry()
        registry.register_command_handler(PlaceOrderHandler())
        bus = DefaultCommandBus(registry=registry, event_publisher=publisher)

        await bus.send(PlaceOrderCommand(customer_id="cust-1", amount=99.0))

        assert len(publisher.calls) == 1
        event, destination = publisher.calls[0]
        assert isinstance(event, OrderPlaced)
        assert destination == "orders.events"

    async def test_handler_without_decorator_passes_none_destination(self) -> None:
        publisher = CapturingPublisher()
        registry = HandlerRegistry()
        registry.register_command_handler(PlaceOrderNoDestHandler())
        bus = DefaultCommandBus(registry=registry, event_publisher=publisher)

        await bus.send(PlaceOrderNoDestCommand(customer_id="cust-2", amount=10.0))

        assert len(publisher.calls) == 1
        _, destination = publisher.calls[0]
        assert destination is None

    async def test_decorator_destination_attribute_set_correctly(self) -> None:
        handler = PlaceOrderHandler()
        assert getattr(handler, "__pyfly_event_destination__", None) == "orders.events"

    async def test_no_decorator_has_no_destination_attribute(self) -> None:
        handler = PlaceOrderNoDestHandler()
        assert getattr(handler, "__pyfly_event_destination__", None) is None
