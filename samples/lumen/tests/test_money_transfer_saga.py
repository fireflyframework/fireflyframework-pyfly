# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""End-to-end tests for the money-transfer saga.

These wire up the **real** framework saga stack (no mocks): the
``ArgumentResolver`` / ``StepInvoker`` / ``SagaExecutionOrchestrator`` /
``SagaCompensator`` / ``SagaEngine`` exactly as
``TransactionalEngineAutoConfiguration`` does, register the real
:class:`MoneyTransferSaga` bean through ``SagaRegistry.register_from_bean``
(mirroring the framework's ``OrchestrationBeanPostProcessor``), and drive it
through :class:`TransferService` over a real :class:`InMemoryWalletRepository`
holding real :class:`Wallet` aggregates.

The two headline behaviours proven here are the ones Chapter 12 quotes:

* a **successful** transfer moves the balance from source to destination;
* a transfer whose **credit step fails** triggers compensation, leaving
  **both** balances at their original values.
"""

from __future__ import annotations

import pytest_asyncio

from lumen.core.services.transfers import (
    MONEY_TRANSFER_SAGA,
    MoneyTransferSaga,
    TransferRequest,
    TransferService,
)
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import InMemoryWalletRepository

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
# Fixtures — the real saga stack, assembled exactly like auto-configuration.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repository() -> InMemoryWalletRepository:
    return InMemoryWalletRepository()


@pytest_asyncio.fixture
async def transfer_service(repository: InMemoryWalletRepository) -> TransferService:
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
    repository: InMemoryWalletRepository,
    *,
    owner_id: str,
    minor: int,
    currency: Currency = Currency.EUR,
) -> Wallet:
    """Create a wallet pre-loaded with ``minor`` units and persist it."""
    wallet = Wallet.open(
        wallet_id=await repository.next_id(),
        owner_id=owner_id,
        currency=currency,
    )
    if minor:
        wallet.deposit(Money(amount=minor, currency=currency))
    wallet.clear_events()
    await repository.add(wallet)
    return wallet


# --------------------------------------------------------------------------
# 1. Happy path — balance moves from source to destination.
# --------------------------------------------------------------------------


async def test_successful_transfer_moves_balance(
    repository: InMemoryWalletRepository,
    transfer_service: TransferService,
) -> None:
    source = await _open_funded_wallet(repository, owner_id="alice", minor=1000)
    destination = await _open_funded_wallet(repository, owner_id="bob", minor=250)

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id=source.id,
            destination_wallet_id=destination.id,
            amount=400,
            currency=Currency.EUR,
        )
    )

    assert result["status"] == "completed"
    assert result["source_balance"] == 600
    assert result["destination_balance"] == 650

    # The persisted aggregates reflect the move: 1000 -> 600, 250 -> 650.
    reloaded_source = await repository.find(source.id)
    reloaded_destination = await repository.find(destination.id)
    assert reloaded_source is not None and reloaded_source.balance.amount == 600
    assert reloaded_destination is not None and reloaded_destination.balance.amount == 650

    # Conservation of money: total across both wallets is unchanged.
    assert reloaded_source.balance.amount + reloaded_destination.balance.amount == 1250


# --------------------------------------------------------------------------
# 2. Compensation — a failing credit leaves BOTH balances untouched.
# --------------------------------------------------------------------------


async def test_failed_credit_compensates_and_leaves_balances_unchanged(
    repository: InMemoryWalletRepository,
    transfer_service: TransferService,
) -> None:
    source = await _open_funded_wallet(repository, owner_id="alice", minor=1000)
    # No destination wallet is created -> the credit-destination step raises
    # AggregateNotFound, which triggers compensation of debit-source.
    missing_destination_id = "wlt-does-not-exist"

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id=source.id,
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
    reloaded_source = await repository.find(source.id)
    assert reloaded_source is not None
    assert reloaded_source.balance.amount == 1000

    # The destination never existed and was never created.
    assert await repository.find(missing_destination_id) is None


async def test_failed_credit_on_currency_mismatch_compensates(
    repository: InMemoryWalletRepository,
    transfer_service: TransferService,
) -> None:
    # Source is EUR; destination is USD. The debit succeeds, but crediting USD
    # with a EUR amount is a currency mismatch the Wallet/Money invariant
    # rejects -> compensation re-credits the source.
    source = await _open_funded_wallet(
        repository, owner_id="alice", minor=1000, currency=Currency.EUR
    )
    destination = await _open_funded_wallet(
        repository, owner_id="bob", minor=500, currency=Currency.USD
    )

    result = await transfer_service.transfer(
        TransferRequest(
            source_wallet_id=source.id,
            destination_wallet_id=destination.id,
            amount=400,
            currency=Currency.EUR,
        )
    )

    assert result["status"] == "failed"
    assert result["compensated_steps"] == ["debit-source"]

    # Both balances are exactly as they started: nothing moved.
    reloaded_source = await repository.find(source.id)
    reloaded_destination = await repository.find(destination.id)
    assert reloaded_source is not None and reloaded_source.balance.amount == 1000
    assert reloaded_destination is not None and reloaded_destination.balance.amount == 500


# --------------------------------------------------------------------------
# 3. The saga is registered and executable by name.
# --------------------------------------------------------------------------


async def test_saga_registered_under_its_name(
    repository: InMemoryWalletRepository,
) -> None:
    registry = SagaRegistry()
    registry.register_from_bean(MoneyTransferSaga(repository=repository))

    definition = registry.get(MONEY_TRANSFER_SAGA)
    assert definition is not None
    assert set(definition.steps.keys()) == {"debit-source", "credit-destination"}
    # The DAG wires credit after debit, and debit declares its compensation.
    assert definition.steps["credit-destination"].depends_on == ["debit-source"]
    assert definition.steps["debit-source"].compensate_name == "recredit_source"
