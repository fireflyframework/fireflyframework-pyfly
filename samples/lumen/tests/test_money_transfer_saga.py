# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end tests for the money-transfer saga.

These wire up the **real** framework saga stack (no mocks): the
``ArgumentResolver`` / ``StepInvoker`` / ``SagaExecutionOrchestrator`` /
``SagaCompensator`` / ``SagaEngine`` exactly as
``TransactionalEngineAutoConfiguration`` does, register the real
:class:`MoneyTransferSaga` bean through ``SagaRegistry.register_from_bean``
(mirroring the framework's ``OrchestrationBeanPostProcessor``), and drive it
through :class:`TransferService` over the real framework
:class:`WalletRepository` against SQLite, holding real :class:`Wallet`
aggregates.

The saga's steps share one ``AsyncSession``; ``upsert`` flushes so each step
(and the assertions) sees the prior step's write.

The two headline behaviours proven here are the ones Chapter 12 quotes:

* a **successful** transfer moves the balance from source to destination;
* a transfer whose **credit step fails** triggers compensation, leaving
  **both** balances at their original values.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.transfers import (
    MONEY_TRANSFER_SAGA,
    MoneyTransferSaga,
    TransferRequest,
    TransferService,
)
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity
from lumen.models.repositories.wallet_repository import WalletRepository
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pyfly.data.relational.sqlalchemy import Base
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import (
    SagaExecutionOrchestrator,
)
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.shared.observability.events import LoggerEventsAdapter
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter

# --------------------------------------------------------------------------
# Fixtures — the real saga stack over a real SQLite-backed repository.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repository() -> AsyncIterator[WalletRepository]:
    """A framework :class:`WalletRepository` over in-memory SQLite.

    One shared session backs both the saga steps and the test assertions,
    so the saga's flushed writes are visible to the reload checks.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield WalletRepository(WalletEntity, session)
    finally:
        await session.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def transfer_service(repository: WalletRepository) -> TransferService:
    """A ``TransferService`` backed by the real saga engine and saga bean."""
    resolver = ArgumentResolver()
    invoker = StepInvoker(argument_resolver=resolver)
    events = LoggerEventsAdapter()
    orchestrator = SagaExecutionOrchestrator(step_invoker=invoker, events_port=events)
    compensator = SagaCompensator(step_invoker=invoker, events_port=events)

    registry = SagaRegistry()
    # The same call the framework's OrchestrationBeanPostProcessor makes at
    # startup for every @saga bean it discovers.
    registry.register_from_bean(MoneyTransferSaga(repository=repository))

    engine = SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=orchestrator,
        compensator=compensator,
        persistence_port=InMemoryPersistenceAdapter(),
        events_port=events,
    )
    return TransferService(saga_engine=engine)


async def _open_funded_wallet(
    repository: WalletRepository,
    *,
    wallet_id: str,
    owner_id: str,
    minor: int,
    currency: Currency = Currency.EUR,
) -> Wallet:
    """Create a wallet pre-loaded with ``minor`` units and persist it."""
    wallet = Wallet.open(wallet_id=wallet_id, owner_id=owner_id, currency=currency)
    if minor:
        wallet.deposit(Money(amount=minor, currency=currency))
    wallet.clear_events()
    await repository.upsert(to_entity(wallet))
    return wallet


async def _balance(repository: WalletRepository, wallet_id: str) -> int | None:
    entity = await repository.find_by_id(wallet_id)
    return entity.balance_minor if entity is not None else None


# --------------------------------------------------------------------------
# 1. Happy path — balance moves from source to destination.
# --------------------------------------------------------------------------


async def test_successful_transfer_moves_balance(
    repository: WalletRepository,
    transfer_service: TransferService,
) -> None:
    await _open_funded_wallet(repository, wallet_id="wlt-src", owner_id="alice", minor=1000)
    await _open_funded_wallet(repository, wallet_id="wlt-dst", owner_id="bob", minor=250)

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id="wlt-src",
            destination_wallet_id="wlt-dst",
            amount=400,
            currency=Currency.EUR,
        )
    )

    assert result["status"] == "completed"
    assert result["source_balance"] == 600
    assert result["destination_balance"] == 650

    # The persisted aggregates reflect the move: 1000 -> 600, 250 -> 650.
    assert await _balance(repository, "wlt-src") == 600
    assert await _balance(repository, "wlt-dst") == 650

    # Conservation of money: total across both wallets is unchanged.
    assert (await _balance(repository, "wlt-src")) + (await _balance(repository, "wlt-dst")) == 1250


# --------------------------------------------------------------------------
# 2. Compensation — a failing credit leaves BOTH balances untouched.
# --------------------------------------------------------------------------


async def test_failed_credit_compensates_and_leaves_balances_unchanged(
    repository: WalletRepository,
    transfer_service: TransferService,
) -> None:
    await _open_funded_wallet(repository, wallet_id="wlt-src", owner_id="alice", minor=1000)
    # No destination wallet is created -> the credit-destination step raises
    # AggregateNotFound, which triggers compensation of debit-source.
    missing_destination_id = "wlt-does-not-exist"

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id="wlt-src",
            destination_wallet_id=missing_destination_id,
            amount=400,
            currency=Currency.EUR,
        )
    )

    # The saga reports failure, names the failing step, and records that the
    # debit was compensated.
    assert result["status"] == "failed"
    assert result["failed_steps"] == ["credit-destination"]
    assert result["compensated_steps"] == ["debit-source"]

    # The crux: compensation re-credited the source, so its balance is back to
    # the original 1000 — the failed transfer moved no money at all.
    assert await _balance(repository, "wlt-src") == 1000

    # The destination never existed and was never created.
    assert await _balance(repository, missing_destination_id) is None


async def test_failed_credit_on_currency_mismatch_compensates(
    repository: WalletRepository,
    transfer_service: TransferService,
) -> None:
    # Source is EUR; destination is USD. The debit succeeds, but crediting USD
    # with a EUR amount is a currency mismatch the Wallet/Money invariant
    # rejects -> compensation re-credits the source.
    await _open_funded_wallet(repository, wallet_id="wlt-src", owner_id="alice", minor=1000, currency=Currency.EUR)
    await _open_funded_wallet(repository, wallet_id="wlt-dst", owner_id="bob", minor=500, currency=Currency.USD)

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id="wlt-src",
            destination_wallet_id="wlt-dst",
            amount=400,
            currency=Currency.EUR,
        )
    )

    assert result["status"] == "failed"
    assert result["compensated_steps"] == ["debit-source"]

    # Both balances are exactly as they started: nothing moved.
    assert await _balance(repository, "wlt-src") == 1000
    assert await _balance(repository, "wlt-dst") == 500


# --------------------------------------------------------------------------
# 3. The saga is registered and executable by name.
# --------------------------------------------------------------------------


async def test_saga_registered_under_its_name(
    repository: WalletRepository,
) -> None:
    registry = SagaRegistry()
    registry.register_from_bean(MoneyTransferSaga(repository=repository))

    definition = registry.get(MONEY_TRANSFER_SAGA)
    assert definition is not None
    assert set(definition.steps.keys()) == {"debit-source", "credit-destination"}
    # The DAG wires credit after debit, and debit declares its compensation.
    assert definition.steps["credit-destination"].depends_on == ["debit-source"]
    assert definition.steps["debit-source"].compensate_name == "recredit_source"
