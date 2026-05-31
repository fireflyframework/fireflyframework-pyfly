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
"""Regression tests for CQRS domain-event publishing wiring.

Guards against the historical bug where ``command_bus`` hardcoded
``event_publisher=NoOpEventPublisher()``, so events emitted by command
handlers were *never* published even when an EDA producer was available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyfly.cqrs.command.bus import DefaultCommandBus
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.config.auto_configuration import CqrsAutoConfiguration
from pyfly.cqrs.config.properties import CqrsProperties
from pyfly.cqrs.event.publisher import (
    EdaCommandEventPublisher,
    NoOpEventPublisher,
)
from pyfly.cqrs.tracing.correlation import CorrelationContext
from pyfly.cqrs.types import Command
from pyfly.eda.ports.outbound import EventHandler, EventPublisher

# -- Fakes ------------------------------------------------------------------


class FakeEventPublisher:
    """In-memory EDA ``EventPublisher`` that records every publish call.

    Implements the real ``pyfly.eda.ports.outbound.EventPublisher`` protocol
    so the wiring is exercised against the genuine port contract.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any], dict[str, str] | None]] = []
        self.started = False
        self.stopped = False

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.published.append((destination, event_type, payload, headers))

    async def start(self) -> None:  # pragma: no cover - unused
        self.started = True

    async def stop(self) -> None:  # pragma: no cover - unused
        self.stopped = True


@dataclass(frozen=True)
class OrderCreated:
    """A simple domain event emitted by the test handler."""

    order_id: str
    amount: float

    @property
    def event_type(self) -> str:
        return "OrderCreated"


@dataclass
class CreateOrderCommand(Command[str]):
    customer_id: str = ""
    amount: float = 0.0
    domain_events: list[Any] = field(default_factory=list)


class CreateOrderHandler(CommandHandler[CreateOrderCommand, str]):
    async def do_handle(self, command: CreateOrderCommand) -> str:
        order_id = f"order-{command.customer_id}"
        command.domain_events.append(OrderCreated(order_id=order_id, amount=command.amount))
        return order_id


# -- Bean wiring ------------------------------------------------------------


def test_command_event_publisher_uses_eda_producer_when_present() -> None:
    cfg = CqrsAutoConfiguration()
    producer = FakeEventPublisher()
    publisher = cfg.command_event_publisher(producer=producer)
    assert isinstance(publisher, EdaCommandEventPublisher)
    assert publisher._producer is producer


def test_command_event_publisher_falls_back_to_noop_without_producer() -> None:
    cfg = CqrsAutoConfiguration()
    publisher = cfg.command_event_publisher()
    assert isinstance(publisher, NoOpEventPublisher)


def test_command_bus_injects_the_publisher_bean() -> None:
    cfg = CqrsAutoConfiguration()
    producer = FakeEventPublisher()
    publisher = cfg.command_event_publisher(producer=producer)
    bus = cfg.command_bus(
        registry=HandlerRegistry(),
        validation=cfg.command_validation_service(cfg.auto_validation_processor()),
        authorization=cfg.authorization_service(CqrsProperties()),
        metrics=cfg.cqrs_metrics_service(),
        event_publisher=publisher,
    )
    assert bus._event_publisher is publisher


# -- End-to-end through the command bus -------------------------------------


async def test_emitted_event_reaches_producer() -> None:
    CorrelationContext.clear()
    producer = FakeEventPublisher()
    publisher = EdaCommandEventPublisher(producer)

    registry = HandlerRegistry()
    registry.register_command_handler(CreateOrderHandler())
    bus = DefaultCommandBus(registry=registry, event_publisher=publisher)

    result = await bus.send(CreateOrderCommand(customer_id="cust-1", amount=42.0))

    assert result == "order-cust-1"
    assert len(producer.published) == 1
    destination, event_type, payload, headers = producer.published[0]
    assert destination == "cqrs.events"
    assert event_type == "OrderCreated"
    assert payload == {"order_id": "order-cust-1", "amount": 42.0}
    assert headers is None


async def test_no_producer_stays_a_silent_noop() -> None:
    CorrelationContext.clear()
    publisher = NoOpEventPublisher()

    registry = HandlerRegistry()
    registry.register_command_handler(CreateOrderHandler())
    bus = DefaultCommandBus(registry=registry, event_publisher=publisher)

    # Must process cleanly even though the emitted event goes nowhere.
    result = await bus.send(CreateOrderCommand(customer_id="cust-2", amount=7.0))
    assert result == "order-cust-2"


# -- DI container optional-injection (the mechanism the bug fix relies on) ---


def test_container_injects_eda_publisher_into_command_event_publisher_bean() -> None:
    """When an ``EventPublisher`` bean exists, the optional param is injected."""
    from pyfly.container.container import Container
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config

    ctx = ApplicationContext(Config({}))
    container: Container = ctx._container

    producer = FakeEventPublisher()
    container.register(FakeEventPublisher)
    container._registrations[FakeEventPublisher].instance = producer
    container.bind(EventPublisher, FakeEventPublisher)

    cfg = CqrsAutoConfiguration()
    publisher = ctx._call_bean_method(cfg, cfg.command_event_publisher)

    assert isinstance(publisher, EdaCommandEventPublisher)
    assert publisher._producer is producer


def test_container_falls_back_to_noop_when_no_eda_publisher_bean() -> None:
    """With no ``EventPublisher`` bean, optional injection yields ``None`` -> NoOp."""
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config

    ctx = ApplicationContext(Config({}))
    cfg = CqrsAutoConfiguration()
    publisher = ctx._call_bean_method(cfg, cfg.command_event_publisher)

    assert isinstance(publisher, NoOpEventPublisher)
