# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""``MoneyTransferSaga`` — an orchestrated saga that moves money between wallets.

This is the canonical *orchestrated saga with compensation*: a central
coordinator (the framework's :class:`SagaEngine`) runs a small DAG of steps
and, when a later step fails, automatically runs the compensating actions for
the steps that already completed — leaving the system as if nothing happened.

The flow is two steps:

1. ``debit-source`` — withdraw the amount from the source wallet, returning a
   :class:`DebitResult`. Its compensation, ``recredit_source``, deposits the
   same amount back.
2. ``credit-destination`` (depends on ``debit-source``) — deposit the amount
   into the destination wallet.

If ``credit-destination`` fails (e.g. the destination wallet does not exist,
or holds a different currency), the engine compensates ``debit-source`` by
re-crediting the source. The net effect of a failed transfer is therefore
**both balances unchanged**.

Wiring notes (verified against ``src/pyfly/transactional``):

* ``@saga(name=...)`` stamps ``__pyfly_saga__`` on the class; ``@service``
  registers it as a DI bean. At startup the framework's
  ``OrchestrationBeanPostProcessor.after_init`` sees the metadata and calls
  ``SagaRegistry.register_from_bean(self)``, so the saga becomes executable by
  name through the auto-configured ``SagaEngine`` — no manual registration.
* ``@saga_step(id=..., compensate=..., depends_on=[...])`` stamps
  ``__pyfly_saga_step__`` directly on the *async* method (no wrapper), so the
  engine awaits it correctly.
* Parameters are injected by ``typing.Annotated`` *marker instances*: forward
  steps take ``Input()`` (the whole ``TransferRequest``) and ``ctx:
  SagaContext`` (by type). A **compensation** method is invoked *without* the
  saga input, so it reads what it needs from its forward step's result via
  ``FromStep("debit-source")`` (the engine threads step results through the
  ``SagaContext``).
* The saga depends only on the hexagonal ``WalletRepository`` *port* and the
  ``Money`` value object — the same domain code the CQRS handlers use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from lumen.core.services.transfers.transfer_request import TransferRequest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.domain import AggregateNotFound
from pyfly.transactional.saga.annotations import FromStep, Input, saga, saga_step
from pyfly.transactional.saga.core.context import SagaContext

#: Saga name used to look the saga up via ``SagaEngine.execute``.
MONEY_TRANSFER_SAGA = "money-transfer"


@dataclass(frozen=True)
class DebitResult:
    """What ``debit-source`` produced — also the input its compensation needs."""

    wallet_id: str
    amount: int
    currency: Currency
    balance: int


@saga(name=MONEY_TRANSFER_SAGA)
@service
class MoneyTransferSaga:
    """Debit the source wallet, credit the destination, compensate on failure.

    Injects the :class:`WalletRepository` *port*; the in-memory adapter (or any
    other) is resolved by the DI container.
    """

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    # -- Step 1: debit the source ----------------------------------------

    @saga_step(id="debit-source", compensate="recredit_source")
    async def debit_source(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> DebitResult:
        """Withdraw ``amount`` from the source wallet; return a DebitResult.

        The :class:`Wallet` aggregate enforces ``balance >= 0`` and refuses a
        currency mismatch, so an invalid debit fails here and the saga never
        touches the destination.
        """
        wallet = await self._repository.find(request.source_wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", request.source_wallet_id)

        wallet.withdraw(Money(amount=request.amount, currency=request.currency))
        await self._repository.add(wallet)
        wallet.clear_events()
        return DebitResult(
            wallet_id=request.source_wallet_id,
            amount=request.amount,
            currency=request.currency,
            balance=wallet.balance.amount,
        )

    async def recredit_source(
        self,
        debit: Annotated[DebitResult, FromStep("debit-source")],
    ) -> int:
        """Compensation for ``debit-source``: put the money back.

        Runs only when a *later* step fails after the debit succeeded. Reads the
        forward step's :class:`DebitResult` via ``FromStep`` (compensation does
        not receive the saga input) and deposits the same amount back into the
        source wallet, restoring its balance.
        """
        wallet = await self._repository.find(debit.wallet_id)
        if wallet is None:  # pragma: no cover - source existed to be debited
            raise AggregateNotFound("Wallet", debit.wallet_id)

        wallet.deposit(Money(amount=debit.amount, currency=debit.currency))
        await self._repository.add(wallet)
        wallet.clear_events()
        return wallet.balance.amount

    # -- Step 2: credit the destination ----------------------------------

    @saga_step(id="credit-destination", depends_on=["debit-source"])
    async def credit_destination(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> int:
        """Deposit ``amount`` into the destination wallet; return its balance.

        If the destination wallet is missing (``AggregateNotFound``) or holds a
        different currency (``BusinessRuleViolation`` from :class:`Money`), this
        raises — the engine then compensates ``debit-source`` via
        ``recredit_source``.
        """
        wallet = await self._repository.find(request.destination_wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", request.destination_wallet_id)

        wallet.deposit(Money(amount=request.amount, currency=request.currency))
        await self._repository.add(wallet)
        wallet.clear_events()
        return wallet.balance.amount
