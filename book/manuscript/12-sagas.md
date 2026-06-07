<span class="eyebrow">Chapter 12</span>

# Distributed Transactions: Sagas, Workflows & TCC {.chtitle}

::: figure art/openers/ch12.svg | &nbsp;

Chapter 10 sent Lumen's wallet events across process boundaries through Kafka. Chapter 11 split the application into co-operating services and showed how to call them over HTTP. Both steps unlocked scale and ownership, but they also uncovered a new kind of danger: you can now have two services, each with its own database, that both need to change state as part of the same business operation — and no distributed ACID transaction to protect you.

Imagine a Lumen "Pay a friend" flow. You debit the sender's wallet in WalletService. You capture the payment in PaymentsService. You send a push notification in NotificationsService. If the payment capture succeeds but the wallet debit fails, you have charged the user without moving money. If the debit succeeds but the notification fails, the recipient does not know about the deposit. You cannot wrap three service calls in a single `BEGIN … COMMIT` — each service owns its own PostgreSQL connection, and two-phase commit across service boundaries is operationally fragile and does not compose at the protocol level.

The answer is **eventual consistency with explicit compensation**. You accept that each step may succeed or fail independently, and you design a recovery path — a *compensating transaction* — for every step that could succeed before a later one fails. When the whole sequence succeeds you have your business result. When any step fails, the engine walks back the steps that already completed, calling each one's compensation to restore consistent state. This chapter shows you how to build that with PyFly's `pyfly.transactional` module.

You will model the money transfer as an **orchestrated saga** — a central class that declares each step and its compensation, using a DAG (directed acyclic graph) of dependencies so the engine can run independent steps in parallel. You will then look at compensation in depth, the **Workflow** pattern for long-running or human-in-the-loop flows, and **TCC (Try-Confirm-Cancel)** as a reservation-based alternative. Finally you will see how pluggable persistence lets the engine survive a process crash and resume stale executions automatically.

---

## The problem with distributed writes

Before writing any code it is worth making the failure modes concrete.

### Two services, no safety net

Lumen's transfer flow touches three services in sequence:

1. `WalletService` — debit the sender (subtract funds).
2. `PaymentsService` — capture the transaction from the sender's card.
3. `NotificationsService` — push an alert to both parties.

In a monolith all three writes could share a single database transaction. In a microservices deployment each service has its own store. A network timeout, a downstream outage, or a logic error in step 2 can leave WalletService having debited the sender while PaymentsService has no record of the charge. The user sees money gone with nothing to show for it.

Retrying the whole operation is not safe: you might debit the sender twice. Skipping failed steps silently violates business rules. You need a principled pattern that commits the forward path step by step and rolls back consistently on failure.

### Eventual consistency and compensation

A **saga** decomposes the operation into a sequence of local transactions, one per service. Each step is durable — it commits to its own store independently. If a step fails, the engine runs **compensating transactions** in reverse order for each step that has already completed. Compensations are not rollbacks in the database sense; they are *semantic undos* — new forward operations that reverse the effect. "Refund a payment" is not a rollback; it is a new debit record on the other direction.

!!! note "Sagas are eventually consistent"
    A saga does not give you serializability or isolation. Between the moment WalletService debits the sender and the moment PaymentsService captures the payment, another request could read a partially-consistent state. This is the trade-off you accept when you choose distributed services. Sagas give you *consistency in the end* — either all forward steps committed or all are compensated — not *consistency at every point*.

---

## An orchestrated saga

PyFly's `pyfly.transactional` module provides the `@saga` and `@saga_step` decorators. You declare one class per saga, annotate each method as a step with its compensation, and declare the dependency ordering. The engine discovers the class via the DI container, builds a validated DAG at startup, and drives execution asynchronously.

### Enabling the engine

Activate the transactional engine by annotating your configuration class:

::: listing lumen/config/app_config.py | Listing 12.1 — Enabling the transactional engine
from pyfly.transactional import enable_transactional_engine
from pyfly.context.conditions import configuration


@enable_transactional_engine
@configuration
class AppConfig:
    pass
:::

**How it works:** `@enable_transactional_engine` triggers `TransactionalEngineAutoConfiguration`, which wires every engine component — `SagaEngine`, `TccEngine`, `SagaRegistry`, `InMemoryPersistenceAdapter`, and `LoggerEventsAdapter` — into the DI container. You can override any adapter bean by providing your own; the auto-configuration uses conditional wiring so your beans take precedence.

### Declaring the transfer saga

The money transfer saga has three steps arranged in a linear chain. Each step depends on its predecessor, so the engine runs them sequentially — exactly what you want when ordering matters.

::: listing lumen/transfer/transfer_saga.py | Listing 12.2 — MoneyTransferSaga: three steps, three compensations
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pyfly.container import component
from pyfly.transactional.saga.annotations import (
    saga,
    saga_step,
    Input,
    FromStep,
    Header,
)
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.result import SagaResult

from lumen.wallet.service import WalletService
from lumen.payments.service import PaymentsService
from lumen.notifications.service import NotificationsService


# ── Domain types ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TransferRequest:
    sender_id: str
    recipient_id: str
    amount_cents: int
    currency: str


@dataclass(frozen=True)
class DebitResult:
    debit_id: str
    new_balance_cents: int


@dataclass(frozen=True)
class CaptureResult:
    transaction_id: str
    charged_cents: int


@dataclass(frozen=True)
class NotifyResult:
    notification_ids: list[str]


# ── Saga definition ───────────────────────────────────────────────────────

@saga(name="money-transfer", layer_concurrency=0)
@component
class MoneyTransferSaga:
    """Orchestrated saga: debit → capture → notify, with full compensation."""

    def __init__(
        self,
        wallet_svc: WalletService,
        payments_svc: PaymentsService,
        notifications_svc: NotificationsService,
    ) -> None:
        self._wallet = wallet_svc
        self._payments = payments_svc
        self._notify = notifications_svc

    # ── Step 1: debit the sender ──────────────────────────────────────────

    @saga_step(
        id="debit-wallet",
        compensate="refund_wallet",
        depends_on=[],
        retry=3,
        backoff_ms=200,
        timeout_ms=5000,
        jitter=True,
        jitter_factor=0.3,
    )
    async def debit_wallet(
        self,
        req: Annotated[TransferRequest, Input],
        ctx: SagaContext,
    ) -> DebitResult:
        return await self._wallet.debit(
            wallet_id=req.sender_id,
            amount=req.amount_cents,
            currency=req.currency,
            idempotency_key=ctx.correlation_id,
        )

    async def refund_wallet(
        self,
        result: Annotated[DebitResult, FromStep("debit-wallet")],
    ) -> None:
        await self._wallet.refund(result.debit_id)

    # ── Step 2: capture payment ───────────────────────────────────────────

    @saga_step(
        id="capture-payment",
        compensate="void_payment",
        depends_on=["debit-wallet"],
        retry=2,
        backoff_ms=500,
        timeout_ms=10_000,
    )
    async def capture_payment(
        self,
        req: Annotated[TransferRequest, Input],
        debit: Annotated[DebitResult, FromStep("debit-wallet")],
        user_id: Annotated[str, Header("X-User-Id")],
    ) -> CaptureResult:
        return await self._payments.capture(
            sender_id=req.sender_id,
            recipient_id=req.recipient_id,
            amount=req.amount_cents,
            debit_ref=debit.debit_id,
        )

    async def void_payment(
        self,
        result: Annotated[CaptureResult, FromStep("capture-payment")],
    ) -> None:
        await self._payments.void(result.transaction_id)

    # ── Step 3: notify both parties ───────────────────────────────────────

    @saga_step(
        id="notify-parties",
        compensate="cancel_notifications",
        depends_on=["capture-payment"],
        retry=1,
        timeout_ms=3_000,
    )
    async def notify_parties(
        self,
        req: Annotated[TransferRequest, Input],
        capture: Annotated[CaptureResult, FromStep("capture-payment")],
    ) -> NotifyResult:
        return await self._notify.transfer_confirmed(
            sender_id=req.sender_id,
            recipient_id=req.recipient_id,
            amount=req.amount_cents,
            transaction_id=capture.transaction_id,
        )

    async def cancel_notifications(
        self,
        result: Annotated[NotifyResult, FromStep("notify-parties")],
    ) -> None:
        for nid in result.notification_ids:
            await self._notify.cancel(nid)
:::

**How it works — step by step:**

`@saga(name="money-transfer")` marks the class for the `SagaRegistry`, which scans the DI container on startup, reads the `__pyfly_saga__` metadata, and builds an internal `SagaDefinition` with a validated DAG. If a `depends_on` entry references a nonexistent step id, or if the graph contains a cycle, `SagaValidationError` is raised immediately — you discover configuration errors at startup rather than at runtime.

`@saga_step` attaches metadata to each method without wrapping it, so `inspect.iscoroutinefunction` continues to work and the engine correctly awaits the call. The `compensate` parameter is the name of the *method on the same class* to call when rolling back this step. `depends_on=["debit-wallet"]` tells the engine that `capture-payment` cannot start until `debit-wallet` finishes; `depends_on=[]` (or omitting it) means the step may run as soon as the engine starts.

`retry=3, backoff_ms=200, jitter=True` — if `debit_wallet` raises any exception, the engine waits `200ms × (1 + random(0, 0.3))` before the next attempt, up to three times. Only after all retries are exhausted does the engine declare the step failed and begin compensating.

Parameter injection uses `typing.Annotated` with marker classes. `Annotated[TransferRequest, Input]` asks the `ArgumentResolver` to inject the full input object you passed to `saga_engine.execute()`. `Annotated[DebitResult, FromStep("debit-wallet")]` reads the result that step `"debit-wallet"` stored in the `SagaContext` when it completed. `Annotated[str, Header("X-User-Id")]` reads a header from the headers dict you passed at execution time. The resolver inspects type hints at runtime via `typing.get_type_hints(func, include_extras=True)`, so no wrapper or proxy is involved.

### The step DAG

The three steps form a linear chain with only one path through the graph:

::: figure art/figures/12-saga.svg | Figure 12.1 — DAG for MoneyTransferSaga: steps run in topological-layer order; each layer executes its steps in parallel when they share no dependency.

```
Layer 0:  debit-wallet
              │
Layer 1:  capture-payment
              │
Layer 2:  notify-parties
```

Because each step depends on exactly one predecessor, there is only one step per layer and no parallelism here. A more complex saga — say, a fraud check and a balance check that are independent, both feeding a payment — would group those two in the same layer and run them with `asyncio.gather`.

### Executing the saga

Inject `SagaEngine` from the DI container and call `execute`:

::: listing lumen/transfer/transfer_service.py | Listing 12.3 — Executing the money transfer saga
from typing import Any

from pyfly.container import service
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.core.result import SagaResult

from lumen.transfer.transfer_saga import TransferRequest


@service
class TransferOrchestrationService:

    def __init__(self, saga_engine: SagaEngine) -> None:
        self._engine = saga_engine

    async def transfer(
        self,
        sender_id: str,
        recipient_id: str,
        amount_cents: int,
        currency: str,
        user_id: str,
    ) -> dict[str, Any]:
        req = TransferRequest(
            sender_id=sender_id,
            recipient_id=recipient_id,
            amount_cents=amount_cents,
            currency=currency,
        )
        result: SagaResult = await self._engine.execute(
            saga_name="money-transfer",
            input_data=req,
            headers={"X-User-Id": user_id},
        )

        if result.success:
            capture = result.result_of("capture-payment")
            return {
                "status": "completed",
                "transaction_id": capture.transaction_id,
                "correlation_id": result.correlation_id,
            }

        failed = result.failed_steps()
        return {
            "status": "failed",
            "failed_steps": list(failed.keys()),
            "error": str(result.error),
            "correlation_id": result.correlation_id,
        }
:::

**How it works:** `saga_engine.execute()` resolves the `MoneyTransferSaga` bean from the registry, creates a `SagaContext` with an auto-generated UUID `correlation_id`, and starts executing layers. On success, `SagaResult.success` is `True` and `result_of("capture-payment")` returns the `CaptureResult` your step returned. On failure, `result.failed_steps()` returns a dict of step id to `StepOutcome` for every step that failed after all retries.

`SagaResult` is an immutable frozen dataclass. In addition to `success` and `error`, it carries:
- `result.steps` — a dict of step id to `StepOutcome` (contains `status`, `attempts`, `latency_ms`, `result`, `error`, `compensated`).
- `result.compensated_steps()` — steps that ran and were then compensated.
- `result.correlation_id` — the UUID you can use to correlate logs and traces across services.

!!! spring "Spring parity"
    `@saga` / `@saga_step` mirror the `@Saga` / `@SagaStep` annotations in the Java `fireflyframework-transactional-engine` library. The parameter-injection markers (`Input`, `FromStep`, `Header`) map directly to `@Input`, `@FromStep`, and `@Header` in the Java version. The async model differs: Java uses Project Reactor (`Mono<T>`) while PyFly uses native `async/await` with `asyncio.gather` for parallel layers — the API surface and configuration model are otherwise identical.

---

## Compensation in depth

The happy path is straightforward: every step succeeds and the saga commits. The interesting design challenge is the unhappy path. Understanding what happens on failure, and why compensation must be designed carefully, is what separates a reliable saga from a brittle one.

### What runs on failure

When a step fails after all retries, the engine switches to *compensation mode*. It inspects the `SagaContext` to find every step whose status is `DONE`, then calls their compensation methods in reverse completion order under the default `STRICT_SEQUENTIAL` policy:

```
Forward path:    debit-wallet ✓  →  capture-payment ✗
Compensation:    refund_wallet (for debit-wallet)
```

If `notify-parties` had also completed before the failure:

```
Forward path:    debit-wallet ✓  →  capture-payment ✓  →  notify-parties ✓  →  [later step fails]
Compensation:    cancel_notifications  →  void_payment  →  refund_wallet
```

Compensation methods receive their arguments through the same injection system as forward steps. `Annotated[DebitResult, FromStep("debit-wallet")]` reads the result that `debit_wallet` stored in the context when it succeeded — so you always compensate with the actual data that was committed, never with an approximation.

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
from pyfly.container import component
from pyfly.transactional.saga.annotations import saga, saga_step


@saga(name="money-transfer-hardened")
@component
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

from pyfly.container import component
from pyfly.transactional.saga.annotations import (
    compensation_step,
    FromStep,
)

from lumen.transfer.transfer_saga import DebitResult
from lumen.wallet.service import WalletService


@compensation_step(saga="money-transfer", for_step_id="debit-wallet")
@component
class WalletRefundCompensation:

    def __init__(self, wallet_svc: WalletService) -> None:
        self._wallet = wallet_svc

    async def execute(
        self,
        result: Annotated[DebitResult, FromStep("debit-wallet")],
    ) -> None:
        await self._wallet.refund(result.debit_id)
:::

The `SagaRegistry` discovers `@compensation_step` classes at startup alongside `@saga` classes and wires them into the saga's step definitions automatically.

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

from pyfly.container import component
from pyfly.transactional.core.model import TriggerMode
from pyfly.transactional.workflow.annotations import (
    workflow,
    workflow_step,
    wait_for_signal,
    compensation_step,
    on_workflow_complete,
    on_workflow_error,
    workflow_query,
)
from pyfly.transactional.workflow.annotations import (
    WaitForSignal as _WFS,
)


@workflow(
    id="large-transfer-approval",
    trigger_mode=TriggerMode.SYNC,
    timeout_ms=86_400_000,    # 24 hours
    max_retries=1,
)
@component
class LargeTransferWorkflow:
    """High-value transfers require a compliance officer to approve."""

    @workflow_step(id="enrich-request", depends_on=[])
    async def enrich_request(self, payload: dict) -> dict:
        # Attach risk score and account metadata
        return {**payload, "risk_score": 0.12}

    @workflow_step(
        id="compliance-review",
        depends_on=["enrich-request"],
        compensatable=True,
        compensation_method="release_review",
        timeout_ms=82_800_000,   # 23 hours
    )
    @wait_for_signal("approved", timeout_ms=82_800_000)
    async def compliance_review(self) -> None:
        """Suspends here until a compliance officer sends the signal."""

    @compensation_step(for_step="compliance-review")
    async def release_review(self) -> None:
        """Called if the workflow is cancelled during review."""

    @workflow_step(
        id="settle-transfer",
        depends_on=["compliance-review"],
    )
    async def settle_transfer(self, payload: dict) -> dict:
        # Call MoneyTransferSaga via SagaEngine
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

**How it works:** `@wait_for_signal("approved", timeout_ms=82_800_000)` stacks on top of `@workflow_step` and tells the engine to *suspend* execution at that step until someone delivers a signal named `"approved"`. The engine persists the `ExecutionContext` and the coroutine state to the configured `ExecutionPersistenceProvider`. If the process restarts, the engine re-hydrates the context and resumes from the last completed layer.

`@compensation_step(for_step="compliance-review")` registers `release_review` as the compensation handler for the `compliance-review` step — the same semantic as saga compensation, but declared at method level rather than via the `compensate=` parameter.

`@workflow_query(name="status")` marks a method as a read-side query handler. You can call it while the workflow is suspended without advancing the execution.

### Driving the workflow engine

::: listing lumen/transfer/approval_controller.py | Listing 12.7 — Starting a workflow and delivering a signal
from __future__ import annotations

from pyfly.container import service
from pyfly.transactional.workflow.engine import WorkflowEngine


@service
class TransferApprovalService:

    def __init__(self, workflow_engine: WorkflowEngine) -> None:
        self._wf = workflow_engine

    async def request_large_transfer(
        self,
        payload: dict,
    ) -> str:
        result = await self._wf.start(
            "large-transfer-approval",
            input=payload,
        )
        # Returns immediately; workflow is suspended at compliance-review
        return result.correlation_id

    async def approve(
        self,
        correlation_id: str,
        reviewer_id: str,
    ) -> None:
        await self._wf.deliver_signal(
            correlation_id,
            "approved",
            payload={"by": reviewer_id},
        )

    async def check_status(self, correlation_id: str) -> str:
        return await self._wf.query(
            correlation_id,
            "status",
        )
:::

**How it works:** `workflow_engine.start()` runs the first layer (`enrich-request`) synchronously, then suspends at `compliance-review` because of the `@wait_for_signal` annotation. It returns a `WorkflowResult` immediately with `correlation_id` — the caller can store this id and poll or await notification. Later, `deliver_signal()` resumes the workflow from the point of suspension; the `settle-transfer` layer then runs to completion.

### The programmatic builder

When you need to construct a workflow dynamically — from a database configuration or a rules engine — use `WorkflowBuilder`:

::: listing lumen/transfer/dynamic_workflow.py | Listing 12.8 — Building a workflow programmatically
from pyfly.transactional.workflow.builder import WorkflowBuilder


async def enrich_fn(payload: dict) -> dict:
    return {**payload, "enriched": True}


async def settle_fn(payload: dict) -> dict:
    return {"settled": True}


definition = (
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

`WorkflowBuilder.step()` accepts a callable and keyword arguments for dependencies, timeouts, and retries. `wait_signal()` inserts a signal-gate step without requiring a real handler — it creates an internal no-op coroutine that the engine replaces with the signal-wait logic. `build()` returns an immutable `WorkflowDefinition` you can register directly with `WorkflowEngine`.

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

from pyfly.container import component
from pyfly.transactional.tcc.annotations import (
    tcc,
    tcc_participant,
    try_method,
    confirm_method,
    cancel_method,
    FromTry,
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
@component
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
from pyfly.container import service
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from pyfly.transactional.tcc.core.result import TccResult

from lumen.transfer.transfer_saga import TransferRequest


@service
class TccTransferService:

    def __init__(self, tcc_engine: TccEngine) -> None:
        self._engine = tcc_engine

    async def transfer(self, req: TransferRequest) -> dict:
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

You started with the insight that a money transfer spanning three services has no distributed rollback. You declared a `MoneyTransferSaga` using `@saga` and `@saga_step`, with parameter injection via `Input`, `FromStep`, and `Header` markers, and a compensation method for every step. You saw how the engine builds a validated DAG at startup, executes steps in topological-layer order (running independent steps in parallel with `asyncio.gather`), and compensates in reverse when any step fails.

You explored compensation in depth: five policies from strict sequential to best-effort parallel, per-step compensation retries and timeouts, and the critical requirement that all compensations be idempotent. You saw how `@wait_for_signal` suspends a `@workflow` until a human or an external event resumes it, and how `WorkflowBuilder` constructs workflows programmatically. You walked through TCC as a reservation-based alternative that locks resources across all participants before committing any. Finally you wired a custom `TransactionalPersistencePort` and configured `SagaRecoveryService` to detect and surface stale executions after a crash.

Key concepts to carry forward:

- **`@saga` / `@saga_step`** — decorator pair that declares the class as a saga and marks each method as a step, with compensation by name and `depends_on` for DAG ordering.
- **Parameter injection** — `Annotated[T, Input]`, `Annotated[T, FromStep("id")]`, `Annotated[str, Header("name")]` resolve from the `SagaContext` at runtime.
- **`SagaEngine.execute(saga_name, input_data, headers)`** — the single call that runs the saga and returns a `SagaResult`.
- **`@workflow` / `@wait_for_signal`** — signal-driven, long-running alternative with built-in persistence after every layer.
- **`@tcc` / `@tcc_participant`** — reservation-based coordination with `@try_method`, `@confirm_method`, `@cancel_method` and `Annotated[T, FromTry()]` injection.
- **`TransactionalPersistencePort`** — implement and register this protocol to give the engine durable state and crash recovery.

---

## Try it yourself {.exercises}

**Exercise 1 — Parallel fraud check.** Add a `check-fraud` step to `MoneyTransferSaga` that calls a `FraudService`. The step should run *in parallel* with `debit-wallet` (no dependency between them) and `capture-payment` should depend on both. Verify the topology with `SagaBuilder` and write a unit test that asserts `result.success` is `True` when both complete.

**Exercise 2 — Compensation error handler.** Change `MoneyTransferSaga`'s compensation policy to `RETRY_WITH_BACKOFF` in YAML. Then deliberately make `refund_wallet` raise `RuntimeError` on the first call and succeed on the second. Write a pytest test using `AsyncMock` that verifies the saga eventually compensates successfully and `result.compensated_steps()` contains `"debit-wallet"`.

**Exercise 3 — Custom persistence.** Implement `TransactionalPersistencePort` backed by a plain Python `dict` that logs every call. Register it as a `@component` and write a test that runs `MoneyTransferSaga`, then calls `get_state(correlation_id)` on your adapter and asserts the recorded `status` is `"COMPLETED"`. Extend the test to simulate a stale saga by manually setting `status = "IN_FLIGHT"` and a past `started_at`, then assert `SagaRecoveryService.recover_stale(stale_threshold_seconds=0)` returns `1`.
