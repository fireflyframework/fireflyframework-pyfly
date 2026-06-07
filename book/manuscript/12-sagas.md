<span class="eyebrow">Chapter 12</span>

# Distributed Transactions: Sagas, Workflows & TCC {.chtitle}

::: figure art/openers/ch12.svg | &nbsp;

Chapter 10 sent Lumen's wallet events across process boundaries through Kafka. Chapter 11 split the application into co-operating services and showed how to call them over HTTP. Both steps unlocked scale and ownership, but they also uncovered a new kind of danger: you can now have multiple aggregates — or multiple services — that all need to change state as part of the same business operation, with no distributed ACID transaction to protect you.

Imagine a Lumen wallet transfer. You debit the source wallet. Then you credit the destination wallet. If the source is debited and the credit fails — wrong currency, missing wallet — the source owner has lost money with nothing deposited on the other side. You cannot wrap two independent repository calls in a single `BEGIN … COMMIT` when each aggregate owns its own consistency boundary, and two-phase commit across independent aggregates is operationally fragile.

The answer is **eventual consistency with explicit compensation**. You accept that each step commits to its own store independently, and you design a recovery path — a *compensating transaction* — for every step that could succeed before a later one fails. When the whole sequence succeeds you have your business result. When any step fails, the engine walks back the steps that already completed, calling each one's compensation to restore consistent state. This chapter shows you how to build that with PyFly's `pyfly.transactional` module.

You will model the money transfer as an **orchestrated saga** — a central class that declares each step and its compensation, using a DAG (directed acyclic graph) of dependencies so the engine can run independent steps in parallel. You will then look at compensation in depth, the **Workflow** pattern for long-running or human-in-the-loop flows, and **TCC (Try-Confirm-Cancel)** as a reservation-based alternative. Finally you will see how pluggable persistence lets the engine survive a process crash and resume stale executions automatically.

---

## The problem with distributed writes

Before writing any code it is worth making the failure modes concrete.

### Two aggregates, no safety net

Lumen's wallet transfer operates on two `Wallet` aggregates that are stored in the same PostgreSQL schema but are independent domain objects — each is loaded, mutated, and saved in its own round trip. Consider the steps:

1. **Debit the source** — withdraw `amount` from the source `Wallet` (enforces `balance >= 0`).
2. **Credit the destination** — deposit `amount` into the destination `Wallet` (enforces currency match).

In a monolith these two writes could share a single database transaction. In the real Lumen domain service each step is an independent repository call. A currency mismatch on the destination wallet, or a missing wallet id, can cause step 2 to fail after step 1 has already committed — leaving the source wallet debited and the destination unchanged. The user loses money.

Retrying the whole operation is not safe: you might debit the source twice. Skipping the failed step silently leaves balances inconsistent. You need a principled pattern that commits each step independently and rolls back all completed steps consistently on failure.

### Eventual consistency and compensation

A **saga** decomposes the operation into a sequence of local transactions, each of which commits to its own store independently. If a step fails, the engine runs **compensating transactions** in reverse order for each step that has already completed. Compensations are not rollbacks in the database sense; they are *semantic undos* — new forward operations that reverse the effect. "Re-credit the source wallet" is not a rollback; it is a new deposit operation that restores the original balance.

!!! note "Sagas are eventually consistent"
    A saga does not give you serializability or isolation. Between the moment the source wallet is debited and the moment the destination wallet is credited, another request could read the source wallet and see a balance that is lower than it will ultimately be. This is the trade-off you accept when you choose to operate across independent aggregates without a distributed lock. Sagas give you *consistency in the end* — either all forward steps committed or all are compensated — not *consistency at every point*.

---

## An orchestrated saga

PyFly's `pyfly.transactional` module provides the `@saga` and `@saga_step` decorators. You declare one class per saga, annotate each method as a step with its compensation, and declare the dependency ordering. The engine discovers the class via the DI container, builds a validated DAG at startup, and drives execution asynchronously.

### Enabling the engine

The transactional engine is activated by the `@enable_domain_stack` starter decorator on your application class, together with one YAML property that turns the engine on. In Lumen:

::: listing lumen/app.py | Listing 12.1 — Enabling the transactional engine via the domain stack
from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.transfers",
        # ... other packages
    ],
)
class LumenApplication:
    pass
:::

Add the property in `application.yaml`:

```yaml
pyfly:
  transactional:
    enabled: true
```

**How it works:** `@enable_domain_stack` imports `TransactionalEngineAutoConfiguration`, which is guarded by `@conditional_on_property("pyfly.transactional.enabled", having_value="true")`. When the property is set, the auto-configuration wires every engine component — `SagaEngine`, `TccEngine`, `WorkflowEngine`, `SagaRegistry`, `InMemoryPersistenceAdapter`, and `LoggerEventsAdapter` — into the DI container. The `OrchestrationBeanPostProcessor` then scans every bean produced during startup: any bean carrying `__pyfly_saga__` metadata is registered into the `SagaRegistry` automatically. You never call `registry.register_from_bean()` yourself in production code.

### Declaring the transfer saga

Lumen's wallet transfer is a two-step saga: debit the source wallet, then credit the destination. If the credit fails (wrong currency, missing wallet), the engine compensates by re-crediting the source — so both balances return to their original values.

::: listing lumen/core/services/transfers/money_transfer_saga.py | Listing 12.2 — MoneyTransferSaga: debit → credit, with compensation
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from lumen.core.services.transfers.transfer_request import TransferRequest
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.domain import AggregateNotFound
from pyfly.transactional.saga.annotations import (
    FromStep,
    Input,
    saga,
    saga_step,
)
from pyfly.transactional.saga.core.context import SagaContext

MONEY_TRANSFER_SAGA = "money-transfer"


@dataclass(frozen=True)
class DebitResult:
    wallet_id: str
    amount: int
    currency: Currency
    balance: int


@saga(name=MONEY_TRANSFER_SAGA)
@service
class MoneyTransferSaga:
    """Debit source wallet, credit destination; compensate on failure."""

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    # -- Step 1: debit the source ----------------------------------------

    @saga_step(id="debit-source", compensate="recredit_source")
    async def debit_source(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> DebitResult:
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
        """Compensation: put the money back. Receives the forward step's
        result via FromStep — NOT the saga input."""
        wallet = await self._repository.find(debit.wallet_id)
        if wallet is None:
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
        wallet = await self._repository.find(request.destination_wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", request.destination_wallet_id)
        wallet.deposit(Money(amount=request.amount, currency=request.currency))
        await self._repository.add(wallet)
        wallet.clear_events()
        return wallet.balance.amount
:::

**How it works — step by step:**

`@saga(name=MONEY_TRANSFER_SAGA)` stamps `__pyfly_saga__` on the class with the saga name. The decorator only attaches metadata — it does not wrap the class or create any proxy. The **critical requirement** is that `@saga` must be stacked *on top of* `@service`. The `@service` annotation causes the DI container to instantiate and scan the bean during application startup; the `OrchestrationBeanPostProcessor.after_init()` hook then sees `__pyfly_saga__` on the bean and calls `SagaRegistry.register_from_bean()`. Without `@service`, the class is never scanned and the saga cannot be executed by name.

`@saga_step` attaches `__pyfly_saga_step__` metadata directly to the async method — no wrapper, no proxy. `inspect.iscoroutinefunction` keeps returning `True` so the engine correctly `await`s the call. The `compensate="recredit_source"` parameter names the *method on the same class* to call when rolling back this step. Omitting `depends_on` (or passing `[]`) means the step can run as soon as the engine starts.

Parameter injection uses `typing.Annotated` with **marker instances**, not bare classes:

- `Annotated[TransferRequest, Input()]` — the `Input()` is an instance (note the parentheses); bare `Input` without `()` does not resolve.
- `Annotated[DebitResult, FromStep("debit-source")]` — reads the result that step `"debit-source"` stored in the `SagaContext` when it completed.
- `ctx: SagaContext` — injected by type; no `Annotated` marker needed.

The resolver inspects type hints at runtime via `typing.get_type_hints(func, include_extras=True)`.

**Compensation methods do not receive the saga input.** `recredit_source` takes `Annotated[DebitResult, FromStep("debit-source")]` — the result the forward step returned — rather than the `TransferRequest`. This is the correct pattern: the compensation always reads from `ctx.step_results` via `FromStep`, never from the original input.

### The step DAG

The two steps form a linear chain:

::: figure art/figures/12-saga.svg | Figure 12.1 — DAG for MoneyTransferSaga: steps run in topological-layer order; independent steps in a layer run with asyncio.gather.

```
Layer 0:  debit-source
              │
Layer 1:  credit-destination
```

Because `credit-destination` depends on `debit-source`, they must run sequentially. A more complex saga — for example, a fraud check and a KYC check that are independent, both feeding a capture step — would put the two checks in the same layer and run them with `asyncio.gather`.

### Executing the saga

Inject `SagaEngine` from the DI container and call `execute`:

::: listing lumen/core/services/transfers/transfer_service.py | Listing 12.3 — Executing the money transfer saga
from __future__ import annotations

from typing import Any

from lumen.core.services.transfers.money_transfer_saga import MONEY_TRANSFER_SAGA
from lumen.core.services.transfers.transfer_request import TransferRequest
from pyfly.container import service
from pyfly.transactional.saga.core.result import SagaResult
from pyfly.transactional.saga.engine.saga_engine import SagaEngine


@service
class TransferService:
    """Run the money-transfer saga and report the outcome."""

    def __init__(self, saga_engine: SagaEngine) -> None:
        self._saga_engine = saga_engine

    async def transfer(self, request: TransferRequest) -> dict[str, Any]:
        result: SagaResult = await self._saga_engine.execute(
            saga_name=MONEY_TRANSFER_SAGA,
            input_data=request,
        )

        if result.success:
            debit = result.result_of("debit-source")
            return {
                "status": "completed",
                "correlation_id": result.correlation_id,
                "source_balance": debit.balance,
                "destination_balance": result.result_of("credit-destination"),
            }

        return {
            "status": "failed",
            "correlation_id": result.correlation_id,
            "failed_steps": list(result.failed_steps().keys()),
            "compensated_steps": list(result.compensated_steps().keys()),
            "error": str(result.error),
        }
:::

**How it works:** `saga_engine.execute()` resolves `MoneyTransferSaga` from the registry by name, creates a `SagaContext` with an auto-generated UUID `correlation_id`, and starts executing layers. On success, `SagaResult.success` is `True` and `result_of("debit-source")` returns the `DebitResult` the forward step produced. On failure, `result.failed_steps()` returns a dict of step id to `StepOutcome` for every step that failed after all retries; `result.compensated_steps()` returns the steps that were successfully rolled back.

`SagaResult` is an immutable frozen dataclass. Its key members:

- `result.success` — `True` when every forward step completed.
- `result.result_of(step_id)` — the value returned by that step, or `None`.
- `result.failed_steps()` — dict of step id → `StepOutcome` for failed steps.
- `result.compensated_steps()` — dict of step id → `StepOutcome` for compensated steps.
- `result.correlation_id` — UUID to correlate logs and traces across services.
- `result.error` — the exception that stopped the saga, or `None` on success.

!!! spring "Spring parity"
    `@saga` / `@saga_step` mirror `@Saga` / `@SagaStep` in the Java `fireflyframework-transactional-engine` library. The decorator-stack rule (`@saga` on `@service`) mirrors the Java rule that `@Saga` must be on a `@Service`-annotated class so the `WorkflowBeanPostProcessor` can discover it. The parameter-injection markers (`Input()`, `FromStep("id")`) map directly to `@Input` and `@FromStep` in the Java version. The async model differs: Java uses Project Reactor (`Mono<T>`) while PyFly uses native `async/await` with `asyncio.gather` for parallel layers.

---

## Compensation in depth

The happy path is straightforward: every step succeeds and the saga commits. The interesting design challenge is the unhappy path. Understanding what happens on failure, and why compensation must be designed carefully, is what separates a reliable saga from a brittle one.

### What runs on failure

When a step fails after all retries, the engine switches to *compensation mode*. It inspects the `SagaContext` to find every step whose status is `DONE`, then calls their compensation methods in reverse completion order under the default `STRICT_SEQUENTIAL` policy. In `MoneyTransferSaga`, the destination wallet not existing causes `credit-destination` to raise `AggregateNotFound`. The engine then compensates the step that already completed:

```
Forward path:  debit-source ✓  →  credit-destination ✗
Compensation:  recredit_source (for debit-source)
```

The net effect: the source wallet is back to its original balance and the destination wallet was never touched — as if the transfer never happened.

Compensation methods receive their arguments through the same injection system as forward steps. `Annotated[DebitResult, FromStep("debit-source")]` reads the `DebitResult` that `debit_source` stored in the context when it completed — so you always compensate with the actual data that was committed, never with an approximation.

### Compensation policies

Five policies control how the engine runs compensations. Set the global default in YAML or override per-execution:

```yaml
pyfly:
  transactional:
    saga:
      compensation_policy: STRICT_SEQUENTIAL
```

| Policy | Behaviour | Use when |
|--------|-----------|----------|
| `STRICT_SEQUENTIAL` | Reverse order, one at a time. Stops on first compensation error. | Ordering matters; partial rollback is unacceptable. |
| `GROUPED_PARALLEL` | Reverses topology layers; compensates each layer in parallel. | You want speed without violating the dependency structure. |
| `RETRY_WITH_BACKOFF` | Reverse order with exponential backoff. Continues if retries succeed. | Transient network failures are likely during compensation. |
| `CIRCUIT_BREAKER` | Tracks consecutive failures; opens after 3 and skips the rest. | Avoid cascading failures; manual recovery handles skipped steps. |
| `BEST_EFFORT_PARALLEL` | All compensations simultaneously; errors logged, never raised. | Speed critical; separate reconciliation handles partial failures. |

!!! warning "Compensation must be idempotent"
    The engine may call a compensation method more than once. If the engine crashes between calling `void_payment` and persisting the compensation result, it will call `void_payment` again on restart. Your compensation methods must be safe to call multiple times with the same arguments. For payment voids, this means the `PaymentsService` should treat a double-void as a no-op (return success if already voided, do not raise). Design compensation *before* designing the forward step — idempotency is not an afterthought.

### Per-step compensation configuration

Override retry and timeout specifically for compensation without changing forward-step behaviour:

::: listing lumen/transfer/transfer_saga_hardened.py | Listing 12.4 — Per-step compensation retry and timeout
from pyfly.container import service
from pyfly.transactional.saga.annotations import saga, saga_step


@saga(name="money-transfer-hardened")
@service
class HardenedTransferSaga:

    @saga_step(
        id="debit-wallet",
        compensate="refund_wallet",
        depends_on=[],
        retry=3,
        backoff_ms=200,
        timeout_ms=5_000,
        compensation_retry=5,
        compensation_backoff_ms=1_000,
        compensation_timeout_ms=8_000,
        compensation_critical=True,
    )
    async def debit_wallet(self, *args: object) -> None: ...

    async def refund_wallet(self, *args: object) -> None: ...
:::

**How it works:** `compensation_retry=5` gives the compensation five attempts of its own, independent of the three forward retries. `compensation_critical=True` means that if the compensation itself exhausts all its retries and still fails, the engine raises the exception from the compensation — this surfaces a *compensation failure* as an observable error rather than silently swallowing it.

### External compensation steps

When the compensation logic is complex enough to warrant its own class, or when it lives in a different module:

::: listing lumen/transfer/compensation_steps.py | Listing 12.5 — External compensation step class
from typing import Annotated

from pyfly.container import service
from pyfly.transactional.saga.annotations import (
    FromStep,
    compensation_step,
)

from lumen.core.services.transfers.money_transfer_saga import DebitResult
from lumen.models.repositories.wallet_repository import WalletRepository


@compensation_step(saga="money-transfer", for_step_id="debit-source")
@service
class SourceRecreditCompensation:

    def __init__(self, repository: WalletRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        debit: Annotated[DebitResult, FromStep("debit-source")],
    ) -> None:
        """External alternative to the inline recredit_source method."""
        ...
:::

The `SagaRegistry` discovers `@compensation_step` classes at startup alongside `@saga` classes and wires them into the saga's step definitions automatically. Note the `for_step_id` parameter matches the step's `id` string exactly.

---

## Workflows and signals

`@saga` is the right tool when you know all the steps upfront and the whole operation should complete within minutes. Some business processes are inherently long-running — a loan approval that waits for a human to review documentation, a multi-step onboarding that waits for an email click, a payment flow that needs a cooldown timer before settling. These fit the **Workflow** pattern.

### How workflows differ from sagas

| | Saga | Workflow |
|---|---|---|
| Duration | Seconds to minutes | Minutes to days |
| Waiting | Retries only | Signals, timers, child workflows |
| Human-in-the-loop | No | Yes (`@wait_for_signal`) |
| State persistence | Per-saga checkpoint | After every layer |
| DAG fan-out | Yes (parallel layers) | Yes + gate primitives |

### Declaring a workflow

::: listing lumen/transfer/approval_workflow.py | Listing 12.6 — LargeTransferWorkflow: signal-driven approval for high-value transfers
from __future__ import annotations

from pyfly.container import service
from pyfly.transactional.core.model import TriggerMode
from pyfly.transactional.workflow.annotations import (
    compensation_step,
    on_workflow_complete,
    on_workflow_error,
    wait_for_signal,
    workflow,
    workflow_query,
    workflow_step,
)


@workflow(
    id="large-transfer-approval",
    trigger_mode=TriggerMode.SYNC,
    timeout_ms=86_400_000,    # 24 hours
    max_retries=1,
)
@service
class LargeTransferWorkflow:
    """High-value transfers require a compliance officer to approve."""

    @workflow_step(id="enrich-request", depends_on=[])
    async def enrich_request(self, payload: dict) -> dict:
        return {**payload, "risk_score": 0.12}

    @workflow_step(
        id="compliance-review",
        depends_on=["enrich-request"],
        compensatable=True,
        compensation_method="release_review",
        timeout_ms=82_800_000,
    )
    @wait_for_signal("approved", timeout_ms=82_800_000)
    async def compliance_review(self) -> None:
        """Suspends until a compliance officer delivers the signal."""

    @compensation_step(for_step="compliance-review")
    async def release_review(self) -> None:
        """Called if the workflow is cancelled during review."""

    @workflow_step(
        id="settle-transfer",
        depends_on=["compliance-review"],
    )
    async def settle_transfer(self, payload: dict) -> dict:
        return {"settled": True}

    @workflow_query(name="status")
    async def get_status(self, ctx: object) -> str:
        return str(getattr(ctx, "status", "UNKNOWN"))

    @on_workflow_complete
    async def on_done(self, ctx: object) -> None:
        pass   # emit audit event

    @on_workflow_error
    async def on_error(self, ctx: object, err: Exception) -> None:
        pass   # alert on-call
:::

**How it works:** The decorator stack follows the same rule as sagas: `@workflow` on top of `@service`. `@workflow(id=...)` takes keyword-only arguments — `id` is required; all others are optional. `@wait_for_signal("approved", timeout_ms=82_800_000)` stacks on top of `@workflow_step` and tells the engine to suspend execution at that step until someone delivers a signal named `"approved"`. The engine persists the `ExecutionContext` to the configured `ExecutionPersistenceProvider`. If the process restarts, the engine re-hydrates the context and resumes from the last completed layer.

`@compensation_step(for_step="compliance-review")` uses the keyword argument `for_step` (not positional). It registers `release_review` as the compensation handler for the `compliance-review` step.

`@workflow_query(name="status")` marks a method as a read-side query handler. You can call it while the workflow is suspended without advancing execution.

### Driving the workflow engine

::: listing lumen/transfer/approval_controller.py | Listing 12.7 — Starting a workflow and delivering a signal
from __future__ import annotations

from pyfly.container import service
from pyfly.transactional.workflow.engine import WorkflowEngine
from pyfly.transactional.workflow.result import WorkflowResult


@service
class TransferApprovalService:

    def __init__(self, workflow_engine: WorkflowEngine) -> None:
        self._wf = workflow_engine

    async def request_large_transfer(self, payload: dict) -> str:
        result: WorkflowResult = await self._wf.start(
            "large-transfer-approval",
            input=payload,
        )
        # Returns immediately; workflow is now suspended at compliance-review.
        return result.correlation_id

    async def approve(self, correlation_id: str, reviewer_id: str) -> None:
        await self._wf.deliver_signal(
            correlation_id,
            "approved",
            payload={"by": reviewer_id},
        )

    async def check_status(self, correlation_id: str) -> str:
        return await self._wf.query(correlation_id, "status")
:::

**How it works:** `workflow_engine.start(workflow_id, input=payload)` runs the first layer (`enrich-request`) synchronously, then suspends at `compliance-review` because of the `@wait_for_signal` annotation. It returns a `WorkflowResult` immediately with `correlation_id` — the caller stores this id and polls later. Later, `deliver_signal()` resumes the workflow; the `settle-transfer` layer then runs to completion.

`WorkflowResult` carries: `workflow_id`, `correlation_id`, `status` (an `ExecutionStatus` enum), `duration_ms`, `step_results` (dict), and `variables`. The boolean `result.successful` is `True` when `status` is `COMPLETED` or `CONFIRMED`.

### The programmatic builder

When you need to construct a workflow dynamically — from a database configuration or a rules engine — use `WorkflowBuilder`:

::: listing lumen/transfer/dynamic_workflow.py | Listing 12.8 — Building a workflow programmatically
from pyfly.transactional.workflow.builder import WorkflowBuilder
from pyfly.transactional.workflow.definition import WorkflowDefinition


async def enrich_fn(payload: dict) -> dict:
    return {**payload, "enriched": True}


async def settle_fn(payload: dict) -> dict:
    return {"settled": True}


definition: WorkflowDefinition = (
    WorkflowBuilder("simple-transfer")
    .step("enrich", enrich_fn, depends_on=[])
    .wait_signal(
        "await-approval",
        "approved",
        depends_on=["enrich"],
        timeout_ms=3_600_000,
    )
    .step(
        "settle",
        settle_fn,
        depends_on=["await-approval"],
    )
    .build()
)
:::

`WorkflowBuilder.step(step_id, handler, *, depends_on, timeout_ms, max_retries, ...)` accepts a callable and keyword arguments for dependencies, timeouts, and retries. `wait_signal(step_id, signal, *, depends_on, timeout_ms)` inserts a signal-gate step without requiring a real handler — it creates an internal no-op coroutine that the engine replaces with the signal-wait logic. `build()` returns a `WorkflowDefinition` you can register directly with `WorkflowEngine`.

---

## TCC: Try-Confirm-Cancel

The saga pattern works by running steps forward and compensating backwards. **TCC (Try-Confirm-Cancel)** takes a different approach: all participants first *tentatively reserve* their resources without committing (Try), then either all *commit* those reservations (Confirm) or all *release* them (Cancel). The distinction matters when you want strong all-or-nothing semantics across participants without a distributed lock.

TCC is a good fit when each participant can cheaply hold a reservation — for example, pre-authorising a payment card hold rather than immediately charging it.

### The three phases

1. **Try** — every participant reserves resources. The reservation is visible internally but not yet final. If any Try fails, all participants that succeeded cancel their reservations.
2. **Confirm** — if all Try phases succeeded, the coordinator instructs every participant to commit its reservation.
3. **Cancel** — if any Try phase failed, the coordinator instructs all participants that completed Try to release their reservations.

### Declaring a TCC transaction

::: listing lumen/transfer/transfer_tcc.py | Listing 12.9 — WalletTransferTcc: Try-Confirm-Cancel for payment reservation
from __future__ import annotations

from typing import Annotated

from pyfly.container import service
from pyfly.transactional.tcc.annotations import (
    FromTry,
    cancel_method,
    confirm_method,
    tcc,
    tcc_participant,
    try_method,
)
from pyfly.transactional.tcc.core.context import TccContext

from lumen.wallet.service import WalletService
from lumen.payments.service import PaymentsService


@tcc(
    name="wallet-transfer",
    timeout_ms=30_000,
    retry_enabled=True,
    max_retries=3,
    backoff_ms=500,
)
@service
class WalletTransferTcc:
    """Reserve funds and payment in lockstep; confirm or cancel together."""

    @tcc_participant(id="wallet-hold", order=1, timeout_ms=5_000)
    class WalletParticipant:

        def __init__(self, wallet_svc: WalletService) -> None:
            self._wallet = wallet_svc

        @try_method(timeout_ms=4_000, retry=2, backoff_ms=200)
        async def try_hold(
            self,
            request: object,
            ctx: TccContext,
        ) -> str:
            """Tentatively hold funds — does not debit yet."""
            return await self._wallet.hold_funds(
                wallet_id=getattr(request, "sender_id", ""),
                amount=getattr(request, "amount_cents", 0),
            )   # returns a hold_id

        @confirm_method(timeout_ms=5_000, retry=3)
        async def confirm_hold(
            self,
            hold_id: Annotated[str, FromTry()],
            ctx: TccContext,
        ) -> None:
            await self._wallet.commit_hold(hold_id)

        @cancel_method(timeout_ms=3_000, retry=2)
        async def cancel_hold(
            self,
            hold_id: Annotated[str, FromTry()],
        ) -> None:
            await self._wallet.release_hold(hold_id)

    @tcc_participant(id="payment-auth", order=2, timeout_ms=8_000)
    class PaymentParticipant:

        def __init__(self, payments_svc: PaymentsService) -> None:
            self._payments = payments_svc

        @try_method(timeout_ms=6_000, retry=2, backoff_ms=300)
        async def try_auth(
            self,
            request: object,
            ctx: TccContext,
        ) -> str:
            return await self._payments.pre_authorise(
                amount=getattr(request, "amount_cents", 0),
            )   # returns auth_id

        @confirm_method(timeout_ms=8_000, retry=3)
        async def confirm_auth(
            self,
            auth_id: Annotated[str, FromTry()],
            ctx: TccContext,
        ) -> None:
            await self._payments.capture_auth(auth_id)

        @cancel_method(timeout_ms=4_000, retry=2)
        async def cancel_auth(
            self,
            auth_id: Annotated[str, FromTry()],
        ) -> None:
            await self._payments.void_auth(auth_id)
:::

**How it works:** `@tcc_participant(order=1)` tells the TCC engine to run `WalletParticipant`'s Try phase before `PaymentParticipant`'s (lower `order` = earlier). `FromTry()` is the TCC equivalent of `FromStep` — it injects the value returned by the same participant's `@try_method` into its `@confirm_method` and `@cancel_method`.

The engine runs all participants' Try phases in `order` sequence. If every Try succeeds, it runs all Confirm methods. If any Try fails, it runs Cancel for every participant that completed its Try — again in the declared order. An `optional=True` participant that fails its Try does not trigger a global Cancel; its failure is logged and skipped.

### Executing a TCC transaction

::: listing lumen/transfer/tcc_service.py | Listing 12.10 — Executing a TCC transaction
from __future__ import annotations

from typing import Any

from pyfly.container import service
from pyfly.transactional.tcc.core.result import TccResult
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine

from lumen.core.services.transfers.transfer_request import TransferRequest


@service
class TccTransferService:

    def __init__(self, tcc_engine: TccEngine) -> None:
        self._engine = tcc_engine

    async def transfer(self, req: TransferRequest) -> dict[str, Any]:
        result: TccResult = await self._engine.execute(
            tcc_name="wallet-transfer",
            input_data=req,
        )

        if result.success:
            hold_id = result.result_of("wallet-hold")
            return {
                "status": "confirmed",
                "hold_id": hold_id,
                "correlation_id": result.correlation_id,
            }

        failed = result.failed_participants()
        return {
            "status": "cancelled",
            "failed": list(failed.keys()),
            "error": str(result.error),
        }
:::

### TCC vs Saga: choosing the right pattern

| Question | Saga | TCC |
|----------|------|-----|
| Steps run independently? | Yes — each commits locally | No — all Try phases must succeed first |
| Needs compensation logic? | Yes, per step | No — Cancel handles rollback |
| Resource reservation needed? | No | Yes — participants hold resources during Try |
| Best for | Long sequential operations | Short all-or-nothing locks |

---

## Persistence: surviving a crash

The engine stores saga and TCC state through the `TransactionalPersistencePort` protocol. The default adapter keeps state in memory — fast for development, but lost on process restart. Production deployments wire a durable adapter.

### How state flows

Every time a step completes (successfully or otherwise), the engine calls:

1. `persistence_port.update_step_status(correlation_id, step_id, status)` — record the step outcome.
2. `persistence_port.mark_completed(correlation_id, successful)` — record the saga's final result.

On startup, the `SagaRecoveryService` queries `persistence_port.get_stale(before)` to find executions that started but never completed. For each stale saga still in `IN_FLIGHT` status, it marks the saga as `FAILED` and emits lifecycle events so observability systems can alert on-call.

### Configuration

```yaml
pyfly:
  transactional:
    saga:
      persistence_enabled: true
      recovery_enabled: true
      recovery_interval_seconds: 60
      stale_threshold_seconds: 600
      cleanup_older_than_hours: 24
```

With `recovery_enabled: true`, the framework runs `SagaRecoveryService.recover_stale()` on a background task every `recovery_interval_seconds` seconds. Sagas last updated more than `stale_threshold_seconds` seconds ago are considered stuck and marked failed — surfacing them for manual investigation or automatic retry at the application level.

### Implementing a custom persistence adapter

To persist to a real database, implement `TransactionalPersistencePort` and register your implementation as a `@bean` or `@component`. The auto-configuration detects your bean at startup and uses it in preference to `InMemoryPersistenceAdapter`:

::: listing lumen/infra/persistence/saga_postgres_adapter.py | Listing 12.11 — Skeleton of a PostgreSQL persistence adapter
from __future__ import annotations

from datetime import datetime
from typing import Any

from pyfly.container import component
from pyfly.transactional.shared.ports.outbound import (
    TransactionalPersistencePort,
)


@component
class SagaPostgresAdapter(TransactionalPersistencePort):

    async def persist_state(self, state: dict[str, Any]) -> None:
        # INSERT INTO saga_executions ...
        ...

    async def get_state(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        # SELECT * FROM saga_executions WHERE ...
        ...

    async def update_step_status(
        self,
        correlation_id: str,
        step_id: str,
        status: str,
    ) -> None: ...

    async def mark_completed(
        self, correlation_id: str, successful: bool
    ) -> None: ...

    async def get_in_flight(self) -> list[dict[str, Any]]:
        return []

    async def get_stale(
        self, before: datetime
    ) -> list[dict[str, Any]]:
        return []

    async def cleanup(self, older_than: datetime) -> int:
        return 0

    async def is_healthy(self) -> bool:
        return True
:::

!!! tip "Use SagaRecoveryService in integration tests"
    In tests that simulate a crash, create a `SagaRecoveryService` with an `InMemoryPersistenceAdapter`, run a saga to a midpoint, manually mark it stale, then call `await recovery.recover_stale(stale_threshold_seconds=0)`. Assert that `SagaResult.success` is `False` and the right steps are marked as failed. This gives you confidence in your recovery logic without spinning up a real database.

---

## The programmatic saga builder

When you want to build a saga from dynamic configuration — perhaps loading step definitions from a rules database or a configuration file — `SagaBuilder` gives you the full fluent API without any decorators:

::: listing lumen/transfer/dynamic_saga.py | Listing 12.12 — Building a saga programmatically with SagaBuilder
from __future__ import annotations

from pyfly.transactional.saga.registry.saga_builder import SagaBuilder
from pyfly.transactional.saga.core.result import SagaResult


async def debit_fn(req: object, ctx: object) -> str:
    return "debit-ref-001"


async def capture_fn(req: object, ctx: object) -> str:
    return "txn-001"


async def refund_fn(result: str) -> None:
    pass   # undo debit


saga_def = (
    SagaBuilder("dynamic-transfer")
    .step("debit")
        .handler(debit_fn)
        .compensate(refund_fn)
        .retry(3)
        .backoff_ms(200)
        .timeout_ms(5_000)
        .jitter(enabled=True, factor=0.3)
        .add()
    .step("capture")
        .handler(capture_fn)
        .depends_on("debit")
        .retry(2)
        .backoff_ms(500)
        .add()
    .layer_concurrency(5)
    .build()
)
:::

**How it works:** Each `.step(step_id)` call returns a `StepBuilder`. You chain configuration methods — `.handler()`, `.compensate()`, `.depends_on()`, `.retry()`, `.backoff_ms()`, `.timeout_ms()`, `.jitter()` — then call `.add()` to finalise the step and return the parent `SagaBuilder`. `.build()` runs the same DAG validation as the decorator path: missing handlers, nonexistent `depends_on` references, and cycles all raise `SagaValidationError` immediately.

---

## What you built {.recap}

You started with the insight that a wallet transfer across two separate database aggregates cannot use a single database transaction. You declared `MoneyTransferSaga` by stacking `@saga` on `@service`, so the `OrchestrationBeanPostProcessor` registers it into the auto-configured `SagaEngine` at startup. Each step uses `Annotated[T, Input()]` and `Annotated[T, FromStep("step-id")]` marker instances (not bare classes) for parameter injection, and `ctx: SagaContext` is injected by type. The compensation method `recredit_source` does not receive the saga input — it pulls the forward step's `DebitResult` via `FromStep("debit-source")` from `SagaContext`. When `credit-destination` raises `AggregateNotFound`, the engine auto-runs `recredit_source`, leaving both balances unchanged.

You explored compensation in depth: five policies from strict sequential to best-effort parallel, per-step compensation retries and timeouts, and the critical requirement that all compensations be idempotent. You saw how `@workflow(id=...) @service` and `@wait_for_signal` suspend a long-running workflow until a human delivers a signal, and how `WorkflowResult.successful` reports the final state. You walked through TCC as a reservation-based alternative that locks resources across all participants before committing any. Finally you wired a custom `TransactionalPersistencePort` and configured `SagaRecoveryService` to detect and surface stale executions after a crash.

Key concepts to carry forward:

- **`@saga` on `@service`** — the decorator stack that makes a class both a DI bean and a registered saga; `@saga` alone is not enough.
- **Marker instances** — `Input()`, `FromStep("step-id")` must be instances (with parentheses), not bare classes.
- **Compensation via `FromStep`** — compensation methods receive the forward step's result, never the saga input.
- **`SagaEngine.execute(saga_name, input_data)`** — the single call that returns `SagaResult` with `.success`, `.result_of()`, `.failed_steps()`, `.compensated_steps()`.
- **`@workflow(id=...) @service` / `@wait_for_signal`** — signal-driven, long-running alternative; `WorkflowResult.successful` for success check.
- **`@tcc` on `@service` / `@tcc_participant`** — reservation-based coordination; `FromTry()` (instance) injects the try-result into confirm/cancel methods.
- **`TransactionalPersistencePort`** — implement and register this protocol to give the engine durable state and crash recovery.

---

## Try it yourself {.exercises}

**Exercise 1 — Parallel balance validation.** Add a `validate-source` step to `MoneyTransferSaga` that checks the source wallet has sufficient funds, without performing the debit. The step should run *in parallel* with nothing (no `depends_on`), and `debit-source` should depend on it. Extend `credit-destination` to depend on `debit-source` as before. Verify the topology via `SagaRegistry.get("money-transfer")` in a test and assert that `definition.steps["debit-source"].depends_on == ["validate-source"]`.

**Exercise 2 — Compensation error handler.** Change `MoneyTransferSaga`'s global compensation policy to `RETRY_WITH_BACKOFF` in `application.yaml`. Then deliberately make `recredit_source` raise `RuntimeError` on the first call and succeed on the second. Write a pytest test using `AsyncMock` on `WalletRepository` that verifies the saga eventually compensates successfully and `result.compensated_steps()` contains `"debit-source"`.

**Exercise 3 — Custom persistence.** Implement `TransactionalPersistencePort` backed by a plain Python `dict` that logs every call. Register it as a `@service` and write a test that runs `TransferService`, then calls `get_state(correlation_id)` on your adapter and asserts the recorded `status` is `"COMPLETED"`. Extend the test to simulate a stale saga by manually setting `status = "IN_FLIGHT"` and a past `started_at`, then assert `SagaRecoveryService.recover_stale(stale_threshold_seconds=0)` returns `1`.
