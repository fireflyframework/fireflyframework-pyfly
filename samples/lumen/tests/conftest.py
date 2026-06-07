# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Test fixtures wiring the full Lumen stack with real components.

The fixtures bring up:

* a real ``InMemoryWalletRepository``
* the real CQRS bus + handler registry, with the real wallet command
  and query handlers registered

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

from lumen.core.services.listeners import WalletAuditListener  # noqa: E402
from lumen.core.services.wallets import (  # noqa: E402
    DepositFundsHandler,
    GetBalanceHandler,
    GetWalletHandler,
    OpenWalletHandler,
    WithdrawFundsHandler,
)
from lumen.models.repositories import InMemoryWalletRepository  # noqa: E402

from pyfly.cqrs import (  # noqa: E402
    DefaultCommandBus,
    DefaultQueryBus,
    HandlerRegistry,
)
from pyfly.eda.adapters.memory import InMemoryEventBus  # noqa: E402


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[InMemoryWalletRepository]:
    yield InMemoryWalletRepository()


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    """A real in-memory EDA bus — the same EventPublisher used in production."""
    yield InMemoryEventBus()


@pytest_asyncio.fixture
async def audit_listener(event_bus: InMemoryEventBus) -> AsyncIterator[WalletAuditListener]:
    """The wallet audit projection, subscribed to the bus exactly as the
    ApplicationContext would auto-wire it at startup."""
    listener = WalletAuditListener()
    # Mirror the context's @event_listener discovery: subscribe the stamped
    # method to each of its declared event-type patterns.
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener


@pytest_asyncio.fixture
async def command_bus(
    repository: InMemoryWalletRepository, event_bus: InMemoryEventBus
) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(
        OpenWalletHandler(repository=repository, events=event_bus)
    )
    registry.register_command_handler(
        DepositFundsHandler(repository=repository, events=event_bus)
    )
    registry.register_command_handler(
        WithdrawFundsHandler(repository=repository, events=event_bus)
    )
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(repository: InMemoryWalletRepository) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    yield DefaultQueryBus(registry=registry)
