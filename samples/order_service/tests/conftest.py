# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Test fixtures wiring the full OrderService stack with real components.

The fixtures bring up:

* a real ``InMemoryOrderRepository``
* the real CQRS bus + handler registry, with the real
  :class:`PlaceOrderHandler` and :class:`GetOrderHandler` registered
* the real saga engine + registry with :class:`ConfirmOrderSaga`
  registered

No mocks. Every behaviour exercised in the tests is the same code path
that runs in production.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

# Make the sample's `src/` importable
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

from order_service.core.services.orders import (  # noqa: E402
    ConfirmOrderSaga,
    GetOrderHandler,
    InventoryService,
    PaymentService,
    PlaceOrderHandler,
    ShippingService,
)
from order_service.models.repositories import InMemoryOrderRepository  # noqa: E402

from pyfly.cqrs import (  # noqa: E402
    DefaultCommandBus,
    DefaultQueryBus,
    HandlerRegistry,
)
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver  # noqa: E402
from pyfly.transactional.saga.engine.compensator import SagaCompensator  # noqa: E402
from pyfly.transactional.saga.engine.execution_orchestrator import (  # noqa: E402
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.saga_engine import SagaEngine  # noqa: E402
from pyfly.transactional.saga.engine.step_invoker import StepInvoker  # noqa: E402
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry  # noqa: E402


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[InMemoryOrderRepository]:
    yield InMemoryOrderRepository()


@pytest_asyncio.fixture
async def command_bus(repository: InMemoryOrderRepository) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(PlaceOrderHandler(repository=repository))
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(repository: InMemoryOrderRepository) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetOrderHandler(repository=repository))
    yield DefaultQueryBus(registry=registry)


@pytest_asyncio.fixture
async def saga_engine(
    repository: InMemoryOrderRepository,
) -> AsyncIterator[tuple[SagaEngine, InventoryService, PaymentService, ShippingService]]:
    inventory = InventoryService()
    payment = PaymentService()
    shipping = ShippingService()
    saga_bean = ConfirmOrderSaga(
        repository=repository,
        inventory=inventory,
        payment=payment,
        shipping=shipping,
    )

    registry = SagaRegistry()
    registry.register_from_bean(saga_bean)

    resolver = ArgumentResolver()
    invoker = StepInvoker(resolver)
    orchestrator = SagaExecutionOrchestrator(invoker)
    compensator = SagaCompensator(invoker)
    engine = SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=orchestrator,
        compensator=compensator,
    )
    yield engine, inventory, payment, shipping
