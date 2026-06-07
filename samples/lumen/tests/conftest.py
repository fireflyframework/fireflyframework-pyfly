# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Test fixtures wiring the full Lumen stack with real components.

No mocks. The fixtures bring up the real persistence and CQRS stack the
application boots on:

* a real SQLite (``aiosqlite``) engine + ``async_sessionmaker``, with the
  schema created from ``Base.metadata`` exactly as the framework's
  ``EngineLifecycle`` does at startup;
* a real framework :class:`WalletRepository` whose derived/spec query
  stubs are compiled by the real ``RepositoryBeanPostProcessor`` — the same
  post-processor the ``ApplicationContext`` runs;
* the real CQRS bus + handler registry, with the wallet command/query
  handlers registered. Command handlers receive the ``async_sessionmaker``
  so their ``@transactional`` boundary opens a committed unit of work.

Every behaviour exercised in the tests is the same code path that runs in
production.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Make the sample's `src/` importable
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
sys.path.insert(0, str(_SRC))

from lumen.core.services.listeners import WalletAuditListener  # noqa: E402
from lumen.core.services.wallets import (  # noqa: E402
    DepositFundsHandler,
    GetBalanceHandler,
    GetWalletHandler,
    ListRichWalletsHandler,
    ListWalletsHandler,
    OpenWalletHandler,
    WithdrawFundsHandler,
)
from lumen.models.entities.v1.wallet_orm import WalletEntity  # noqa: E402,F401
from lumen.models.repositories import WalletRepository  # noqa: E402

from pyfly.cqrs import (  # noqa: E402
    DefaultCommandBus,
    DefaultQueryBus,
    HandlerRegistry,
)
from pyfly.data.relational.sqlalchemy import Base  # noqa: E402
from pyfly.data.relational.sqlalchemy.post_processor import (  # noqa: E402
    RepositoryBeanPostProcessor,
)
from pyfly.eda.adapters.memory import InMemoryEventBus  # noqa: E402


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """An in-memory SQLite engine + session factory, schema created.

    Mirrors the framework's relational auto-configuration: build the async
    engine and run ``Base.metadata.create_all``. A single shared engine
    keeps the in-memory database alive for the whole test.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[WalletRepository]:
    """The framework :class:`WalletRepository`, post-processed.

    A single shared session backs reads/queries — exactly the shape the
    container injects (one ``async_session`` bean). Command handlers swap a
    per-unit-of-work session onto it via ``@transactional`` using the same
    ``session_factory``.
    """
    session = session_factory()
    repo = WalletRepository(WalletEntity, session)
    # Mirror the context: compile the derived-query stubs onto the bean.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    try:
        yield repo
    finally:
        await session.close()


@pytest_asyncio.fixture
async def event_bus() -> AsyncIterator[InMemoryEventBus]:
    """A real in-memory EDA bus — the same EventPublisher used in production."""
    yield InMemoryEventBus()


@pytest_asyncio.fixture
async def audit_listener(event_bus: InMemoryEventBus) -> AsyncIterator[WalletAuditListener]:
    """The wallet audit projection, subscribed to the bus exactly as the
    ApplicationContext would auto-wire it at startup."""
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener


@pytest_asyncio.fixture
async def command_bus(
    repository: WalletRepository,
    event_bus: InMemoryEventBus,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[DefaultCommandBus]:
    registry = HandlerRegistry()
    registry.register_command_handler(
        OpenWalletHandler(repository=repository, events=event_bus, session_factory=session_factory)
    )
    registry.register_command_handler(
        DepositFundsHandler(repository=repository, events=event_bus, session_factory=session_factory)
    )
    registry.register_command_handler(
        WithdrawFundsHandler(repository=repository, events=event_bus, session_factory=session_factory)
    )
    yield DefaultCommandBus(registry=registry)


@pytest_asyncio.fixture
async def query_bus(repository: WalletRepository) -> AsyncIterator[DefaultQueryBus]:
    registry = HandlerRegistry()
    registry.register_query_handler(GetWalletHandler(repository=repository))
    registry.register_query_handler(GetBalanceHandler(repository=repository))
    registry.register_query_handler(ListWalletsHandler(repository=repository))
    registry.register_query_handler(ListRichWalletsHandler(repository=repository))
    yield DefaultQueryBus(registry=registry)
