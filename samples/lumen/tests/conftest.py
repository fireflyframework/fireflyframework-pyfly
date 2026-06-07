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


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[InMemoryWalletRepository]:
    yield InMemoryWalletRepository()


@pytest_asyncio.fixture
async def command_bus(repository: InMemoryWalletRepository) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(OpenWalletHandler(repository=repository))
    registry.register_command_handler(DepositFundsHandler(repository=repository))
    registry.register_command_handler(WithdrawFundsHandler(repository=repository))
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(repository: InMemoryWalletRepository) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    yield DefaultQueryBus(registry=registry)
