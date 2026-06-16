<span class="eyebrow">Chapter 12</span>

# Distributed Transactions: Sagas, Workflows & TCC {.chtitle}

::: figure art/openers/ch12.svg | &nbsp;

Chapter 10 sent Lumen's wallet events across process boundaries through
Kafka. Chapter 11 split the application into co-operating services and
showed how to call them over HTTP. Both steps unlocked scale and team
autonomy, but they also exposed a new class of danger: multiple aggregates
— or multiple services — may all need to change state as part of one
business operation, with no distributed ACID transaction to protect you.

Picture a Lumen wallet transfer. You debit the source wallet, then credit
the destination. If the source is debited and the credit fails — wrong
currency, missing wallet — the source owner loses money with nothing
deposited on the other side. You cannot wrap two independent repository
calls in a single `BEGIN … COMMIT` when each aggregate owns its own
consistency boundary, and two-phase commit across independent aggregates
is operationally fragile.

The answer is **eventual consistency with explicit compensation**. Each
step commits to its own store independently, and you design a recovery
path — a **compensating transaction** — for every step that could succeed
before a later one fails. When the whole sequence succeeds you have your
business result; when any step fails, the engine walks back the completed
steps in reverse, calling each compensation to restore consistent state.
This chapter shows you how to build that with PyFly's
`pyfly.transactional` module.

You will model the money transfer as an **orchestrated saga** — a central
class that declares each step and its compensation, organised in a DAG
(directed acyclic graph) so the engine can run independent steps in
parallel. You will then explore compensation in depth, the **Workflow**
pattern for long-running or human-in-the-loop flows, and **TCC
(Try-Confirm-Cancel)** as a reservation-based alternative. A closing
section shows how pluggable persistence lets the engine survive a process
crash and automatically resume stale executions.

---

## The problem with distributed writes

Before writing any code, make the failure modes concrete.

### Two aggregates, no safety net

Lumen's wallet transfer operates on two `Wallet` aggregates stored in the
same PostgreSQL schema but treated as independent domain objects — each is
loaded, mutated, and saved in its own round trip. The two steps are:

1. **Debit the source** — withdraw `amount` from the source `Wallet` (enforces `balance >= 0`).
2. **Credit the destination** — deposit `amount` into the destination `Wallet` (enforces currency match).

In a monolith, both writes could share one database transaction. In the
Lumen domain service each step is an independent repository call. A
currency mismatch on the destination, or a missing wallet ID, causes step 2
to fail after step 1 has already committed — leaving the source wallet
debited and the destination unchanged. The user loses money.

Retrying the whole operation is unsafe: you might debit the source twice.
Silently skipping the failed step leaves balances inconsistent. You need a
principled pattern that commits each step independently and rolls back every
completed step consistently on failure.

### Eventual consistency and compensation

A **saga** decomposes the operation into a sequence of local transactions,
each committing to its own store independently. When a step fails, the
engine runs **compensating transactions** in reverse order for every
completed step. Compensations are not database rollbacks; they are
*semantic undos* — new forward operations that reverse the effect.
"Re-credit the source wallet" is a new deposit operation that restores the
original balance, not a rollback.

!!! note "Sagas are eventually consistent"
    A saga does not give you serializability or isolation. Between the moment the source wallet is debited and the moment the destination wallet is credited, another request could read the source wallet and see a balance that is lower than it will ultimately be. This is the trade-off you accept when you choose to operate across independent aggregates without a distributed lock. Sagas give you *consistency in the end* — either all forward steps committed or all are compensated — not *consistency at every point*.

---

## An orchestrated saga

PyFly's `pyfly.transactional` module provides the `@saga` and
`@saga_step` decorators. You declare one class per saga, annotate each
method as a step with its compensation, and declare the dependency
ordering. The engine discovers the class through the DI container, builds
a validated DAG at startup, and drives execution asynchronously.

!!! note "New term: orchestration"
    *Orchestration* means one central component — here, PyFly's `SagaEngine` — decides the order in which steps run and what to do when one fails. The alternative, *choreography*, has each service react to events with no central conductor. This chapter uses orchestration because it makes the recovery path explicit and easy to test: the engine owns the rules, your saga class just declares the steps.

We will build the transfer saga in four moves: turn the engine on, declare
the saga class, look at the DAG the engine builds from it, then call the
engine from a service. Take them one at a time.

### Enabling the engine

The transactional engine is activated by the `@enable_domain_stack`
starter decorator on your application class — and that single decorator is
all you need. No extra YAML is required. In Lumen:

**Add the starter decorator.** Open your application class and
stack `@enable_domain_stack` above `@pyfly_application`. A *starter*
decorator is PyFly's way of switching on a whole feature area (here, the
transactional engine and its DI wiring) without you registering each
component by hand — the Spring equivalent is an `@EnableXxx` annotation.

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

**Turning it off, or on under a narrower starter.** `@enable_domain_stack`
already sets `pyfly.transactional.enabled: true` for you, so the engine is
live as soon as the decorator is on the class. The same property is the
knob you reach for in two situations:

- **To switch the engine off** under the domain stack, set it to `false` in
  `application.yaml` — the auto-configuration is gated on the value being
  exactly `"true"`, so anything else disables it.
- **To switch it on under a narrower starter** such as `@enable_core_stack`
  (which does *not* include the transactional engine), add the property
  yourself:

```yaml
pyfly:
  transactional:
    enabled: true
```

!!! note "Run it: confirm the engine wired up"
    Start the app on its default port (`pyfly.server.port` is `8080` in v26.6.110) and watch the startup log:

    ```bash
    uv run pyfly run
    ```

    Among the startup lines you should see the transactional components register, for example:

    ```
    INFO  pyfly.starters.domain  domain stack enabled: transactional engine active
    INFO  pyfly.transactional    registered saga 'money-transfer' (2 steps)
    INFO  pyfly.server           Uvicorn running on http://0.0.0.0:8080
    ```

    If you explicitly set `transactional.enabled: false` (or enable only the core stack without adding the property), the saga line never appears and `SagaEngine.execute(...)` later raises `ValueError: Saga 'money-transfer' is not registered`. Seeing the `registered saga` line is your proof the wiring worked.

**How it works:** `@enable_domain_stack` merges `DOMAIN_STACK_PROPERTIES`
into the active config, and that dict already contains
`pyfly.transactional.enabled: "true"` — so the decorator both registers
*and* activates the engine in one move. The auto-configuration,
`TransactionalEngineAutoConfiguration`, is guarded by
`@conditional_on_property("pyfly.transactional.enabled", having_value="true")`;
because the starter set the value to `"true"`, the condition matches and
the auto-configuration wires every engine component — `SagaEngine`,
`TccEngine`, `WorkflowEngine`, `SagaRegistry`,
`InMemoryPersistenceAdapter`, and `LoggerEventsAdapter` — into the DI
container. The `OrchestrationBeanPostProcessor` then scans every bean
produced at startup: any bean carrying `__pyfly_saga__` metadata is
registered into `SagaRegistry` automatically. You never call
`registry.register_from_bean()` in production code.

!!! note "What just happened"
    One small change — a single decorator — gave you a fully wired saga engine. `@enable_domain_stack` both declared the components and turned them on (it sets `pyfly.transactional.enabled: true` for you), and a startup bean post-processor found your saga classes and registered them for you. From here on you only write saga classes and call `SagaEngine.execute(...)`; the plumbing is done.

### Declaring the transfer saga

Lumen's wallet transfer is a two-step saga: debit the source wallet, then
credit the destination. If the credit fails — wrong currency or missing
wallet — the engine compensates by re-crediting the source, returning both
balances to their original values.

!!! note "New term: compensation"
    A *compensation* (or *compensating transaction*) is the undo for a step. It is not a database rollback — by the time you compensate, the original write has already committed to its own store. Instead it is a *new forward operation* that semantically reverses the effect. The undo for "debit the source" is not `ROLLBACK`; it is "deposit the same amount back into the source". Every step that changes state needs a matching compensation.

Build it in three moves. **Step 1** — declare the class and stack the
decorators. **Step 2** — write the forward steps and their compensation.
**Step 3** — wire the parameters with injection markers. The complete file
is below; we then walk each move.

::: listing lumen/core/services/transfers/money_transfer_saga.py | Listing 12.2 — MoneyTransferSaga: debit → credit, with compensation
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from lumen.core.mappers.wallet_mapper import to_aggregate, to_entity
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
        entity = await self._repository.find_by_id(
            request.source_wallet_id
        )
        if entity is None:
            raise AggregateNotFound("Wallet", request.source_wallet_id)
        wallet = to_aggregate(entity)
        wallet.withdraw(
            Money(amount=request.amount, currency=request.currency)
        )
        await self._repository.upsert(to_entity(wallet))
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
        entity = await self._repository.find_by_id(debit.wallet_id)
        if entity is None:
            raise AggregateNotFound("Wallet", debit.wallet_id)
        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=debit.amount, currency=debit.currency)
        )
        await self._repository.upsert(to_entity(wallet))
        wallet.clear_events()
        return wallet.balance.amount

    # -- Step 2: credit the destination ----------------------------------

    @saga_step(id="credit-destination", depends_on=["debit-source"])
    async def credit_destination(
        self,
        request: Annotated[TransferRequest, Input()],
        ctx: SagaContext,
    ) -> int:
        entity = await self._repository.find_by_id(
            request.destination_wallet_id
        )
        if entity is None:
            raise AggregateNotFound(
                "Wallet", request.destination_wallet_id
            )
        wallet = to_aggregate(entity)
        wallet.deposit(
            Money(amount=request.amount, currency=request.currency)
        )
        await self._repository.upsert(to_entity(wallet))
        wallet.clear_events()
        return wallet.balance.amount
:::

**How it works — step by step:**

**Step 1 — the decorator stack.**
`@saga(name=MONEY_TRANSFER_SAGA)` stamps `__pyfly_saga__` on the class
with the saga name. The decorator only attaches metadata — it does not
wrap the class or create a proxy. **The critical requirement** is that
`@saga` must be stacked *on top of* `@service`. The `@service` annotation
causes the DI container to instantiate and scan the bean at startup; the
`OrchestrationBeanPostProcessor.after_init()` hook then sees
`__pyfly_saga__` on the bean and calls `SagaRegistry.register_from_bean()`.
Without `@service`, the class is never scanned and the saga cannot be
executed by name.

!!! warning "Decorator order is not optional"
    Read the stack top-to-bottom: `@saga` is *above* `@service`. Swap them — `@service` above `@saga` — and the bean still registers with DI, but the saga metadata is applied to the already-wrapped object, the post-processor never finds it, and `execute("money-transfer")` fails with `ValueError: Saga 'money-transfer' is not registered`. If you hit that error, check the decorator order first.

**Step 2 — the step methods.**
`@saga_step` attaches `__pyfly_saga_step__` metadata directly to the
async method — no wrapper, no proxy — so `inspect.iscoroutinefunction`
keeps returning `True` and the engine correctly `await`s the call. The
`compensate="recredit_source"` parameter names the *method on the same
class* to invoke when rolling back this step. Omitting `depends_on` (or
passing `[]`) means the step can run as soon as the engine starts.

**Repository interaction — the load-mutate-save cycle.** Each step
follows the same three-phase pattern, using the framework
`WalletRepository(Repository[WalletEntity, str])`:

1. `find_by_id(id)` — loads the raw `WalletEntity` row from the database.
2. `to_aggregate(entity)` — rehydrates the rich `Wallet` aggregate from
   that row; the aggregate enforces all invariants (`balance >= 0`,
   currency match).
3. Mutate — call `wallet.withdraw(...)` or `wallet.deposit(...)` on the
   aggregate, letting it raise `BusinessRuleViolation` if an invariant
   is broken before any write occurs.
4. `upsert(to_entity(wallet))` — flattens the mutated aggregate back to a
   `WalletEntity` and calls `session.merge` + `flush`, so the write is
   visible to subsequent steps in the same `AsyncSession` without
   committing.

Because saga steps share one `AsyncSession`, `upsert` flushes so each
step sees the previous step's write; the surrounding application boundary
owns the final commit.

**Step 3 — wire the parameters.**
Parameter injection uses `typing.Annotated` with **marker instances**, not
bare classes:

- `Annotated[TransferRequest, Input()]` — `Input()` is an instance (note
  the parentheses); bare `Input` without `()` does not resolve.
- `Annotated[DebitResult, FromStep("debit-source")]` — reads the result
  that step `"debit-source"` stored in `SagaContext` when it completed.
- `ctx: SagaContext` — injected by type; no `Annotated` marker needed.

The resolver inspects type hints at runtime via
`typing.get_type_hints(func, include_extras=True)`.

**Compensation methods do not receive the saga input.** `recredit_source`
takes `Annotated[DebitResult, FromStep("debit-source")]` — the value the
forward step returned — not the `TransferRequest`. It re-loads the entity
via `find_by_id`, rehydrates the aggregate, deposits the original amount
back, and upserts — the same load-mutate-save cycle as the forward steps.
Compensations always read from `ctx.step_results` via `FromStep`, never
from the original input.

!!! note "What just happened"
    You wrote one class that holds the whole transfer story: a forward step to debit, its compensation to re-credit, and a second forward step to credit. The decorators told the engine *what each method is* (a step, a compensation) and *how they connect* (`compensate=`, `depends_on=`). You did not write any orchestration loop or try/except rollback logic — that is the engine's job. Your code only describes the business operation and its undo.

### The step DAG

!!! note "New term: DAG"
    A *DAG* — directed acyclic graph — is a set of steps connected by "must-run-before" arrows, with no cycles (no step can, directly or indirectly, depend on itself). The engine reads your `depends_on` declarations, builds this graph, and sorts it into *layers*: everything in layer 0 has no unmet dependencies and runs first; layer 1 runs once layer 0 finishes; and so on. Steps in the same layer are independent, so the engine runs them at the same time. A cycle would make the layering impossible, so the engine rejects it at startup rather than at run time.

The two steps form a linear chain:

::: figure art/figures/12-saga.svg | Figure 12.1 — DAG for MoneyTransferSaga: steps run in topological-layer order; independent steps in a layer run with asyncio.gather.

```
Layer 0:  debit-source
              │
Layer 1:  credit-destination
```

Because `credit-destination` depends on `debit-source`, they run
sequentially. A more complex saga — a fraud check and a KYC check that are
independent of each other but both feed a capture step — would place the
two checks in the same layer and run them concurrently with
`asyncio.gather`.

### Executing the saga

The saga class only *describes* the operation. To *run* it you need a thin
service that injects the engine and calls it by name.

**Step 1 — inject `SagaEngine`.** Declare a `@service` whose constructor
takes a `SagaEngine` parameter; the DI container hands you the
auto-configured engine. **Step 2 — call `execute`** with the saga name and
the input payload. **Step 3 — fold the `SagaResult`** into a small,
JSON-friendly dict for the caller.

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

**How it works:** `saga_engine.execute()` resolves `MoneyTransferSaga`
from the registry by name, creates a `SagaContext` with an auto-generated
UUID `correlation_id`, and starts executing layers. On success,
`SagaResult.success` is `True` and `result_of("debit-source")` returns the
`DebitResult` the forward step produced. On failure, `result.failed_steps()`
returns a dict of step ID to `StepOutcome` for every step that exhausted
its retries; `result.compensated_steps()` returns the steps that were
successfully rolled back.

`SagaResult` is an immutable frozen dataclass. Its key members:

- `result.success` — `True` when every forward step completed.
- `result.result_of(step_id)` — the value that step returned, or `None`.
- `result.failed_steps()` — dict of step ID → `StepOutcome` for failed steps.
- `result.compensated_steps()` — dict of step ID → `StepOutcome` for compensated steps.
- `result.correlation_id` — UUID to correlate logs and traces across services.
- `result.error` — the exception that stopped the saga, or `None` on success.

!!! note "Run it: the happy path and the compensated path"
    Expose `TransferService.transfer` behind an HTTP route (Chapter 11 covered controllers) and exercise both outcomes against the running app on `pyfly.server.port` (`8080`).

    A valid transfer between two existing same-currency wallets returns the completed summary:

    ```bash
    curl -s -X POST http://localhost:8080/transfers \
      -d '{"source_wallet_id":"w-1","destination_wallet_id":"w-2","amount":500,"currency":"EUR"}'
    ```

    ```json
    {
      "status": "completed",
      "correlation_id": "8f3c…",
      "source_balance": 9500,
      "destination_balance": 10500
    }
    ```

    Now point the transfer at a destination wallet that does not exist. `credit-destination` raises `AggregateNotFound`, the engine compensates `debit-source`, and the response reports exactly that:

    ```bash
    curl -s -X POST http://localhost:8080/transfers \
      -d '{"source_wallet_id":"w-1","destination_wallet_id":"does-not-exist","amount":500,"currency":"EUR"}'
    ```

    ```json
    {
      "status": "failed",
      "correlation_id": "1a2b…",
      "failed_steps": ["credit-destination"],
      "compensated_steps": ["debit-source"],
      "error": "Wallet 'does-not-exist' not found"
    }
    ```

    The key observation: re-read `w-1` afterwards and its balance is back to `9500` — `debit-source` was rolled back by `recredit_source`. A failed transfer leaves both wallets exactly as they started.

!!! spring "Spring parity"
    `@saga` / `@saga_step` mirror `@Saga` / `@SagaStep` in the Java `fireflyframework-transactional-engine` library. The decorator-stack rule (`@saga` on `@service`) mirrors the Java rule that `@Saga` must be on a `@Service`-annotated class so the `WorkflowBeanPostProcessor` can discover it. The parameter-injection markers (`Input()`, `FromStep("id")`) map directly to `@Input` and `@FromStep` in the Java version. The async model differs: Java uses Project Reactor (`Mono<T>`) while PyFly uses native `async/await` with `asyncio.gather` for parallel layers.

---

## Compensation in depth

The happy path is straightforward: every step succeeds and the saga
commits. The real design challenge is the unhappy path. Understanding what
happens on failure — and why compensation must be designed carefully — is
what separates a reliable saga from a brittle one.

### What runs on failure

When a step fails after all retries, the engine enters *compensation mode*.
It inspects `SagaContext` for every step whose status is `DONE`, then calls
their compensation methods in reverse completion order under the default
`STRICT_SEQUENTIAL` policy. In `MoneyTransferSaga`, a missing destination
wallet causes `credit-destination` to raise `AggregateNotFound`. The engine
then compensates the step that already completed:

```
Forward path:  debit-source ✓  →  credit-destination ✗
Compensation:  recredit_source (for debit-source)
```

The net effect: the source wallet is restored to its original balance and
the destination wallet was never touched — as if the transfer never
happened.

Compensation methods receive their arguments through the same injection
system as forward steps. `Annotated[DebitResult, FromStep("debit-source")]`
reads the `DebitResult` that `debit_source` stored in the context when it
completed — so you always compensate with the actual committed data, never
an approximation.

!!! note "Run it: prove compensation in a test"
    You do not need a running server to verify the unhappy path — a fast unit test against the engine is enough, and it is the kind of test you will write for every saga. Drive `TransferService.transfer` with a destination wallet that does not exist, then assert the compensation ran:

    ```python
    async def test_failed_transfer_compensates_the_debit(transfer_service):
        result = await transfer_service.transfer(
            TransferRequest(
                source_wallet_id="w-1",
                destination_wallet_id="does-not-exist",
                amount=500,
                currency=Currency.EUR,
            )
        )
        assert result["status"] == "failed"
        assert result["failed_steps"] == ["credit-destination"]
        assert result["compensated_steps"] == ["debit-source"]
    ```

    Run just this test (the `--extra dev` group installs pytest; Chapter 16 covers fixtures in depth):

    ```bash
    uv run --extra dev pytest -q -k compensates
    ```

    Expected output:

    ```
    1 passed in 0.05s
    ```

    A green test here is your guarantee that a broken transfer never leaves money missing.

### Compensation policies

Five policies govern how the engine runs compensations. Set the global
default in YAML or override it per execution:

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

You can override retry count and timeout for a compensation step without
changing forward-step behaviour:

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

**How it works:** `compensation_retry=5` gives the compensation five
attempts of its own, independent of the three forward-step retries.
`compensation_critical=True` means that if the compensation exhausts all
its retries and still fails, the engine raises that exception — surfacing
the *compensation failure* as an observable error rather than silently
swallowing it.

### External compensation steps

When compensation logic is complex enough to warrant its own class, or when
it lives in a different module, move it out entirely:

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

The `SagaRegistry` discovers `@compensation_step` classes at startup
alongside `@saga` classes and wires them into their step definitions
automatically. The `for_step_id` parameter must match the step's `id`
string exactly.

---

## Workflows and signals

`@saga` is the right tool when all steps are known upfront and the
operation completes within minutes. Some business processes are inherently
longer — a loan approval waiting on a compliance officer, a multi-step
onboarding blocked on an email click, a payment that needs a cooldown
before settling. These fit the **Workflow** pattern.

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

**How it works:** The decorator stack follows the same rule as sagas:
`@workflow` on top of `@service`. `@workflow(id=...)` takes keyword-only
arguments — `id` is required; all others are optional.
`@wait_for_signal("approved", timeout_ms=82_800_000)` stacks on top of
`@workflow_step` and tells the engine to suspend at that step until a
signal named `"approved"` is delivered. The engine persists the
`ExecutionContext` to the configured `ExecutionPersistenceProvider`; if
the process restarts, it re-hydrates the context and resumes from the
last completed layer.

`@compensation_step(for_step="compliance-review")` uses the keyword
argument `for_step` (not positional) and registers `release_review` as
the compensation handler for the `compliance-review` step.

`@workflow_query(name="status")` marks a method as a read-side query
handler — callable while the workflow is suspended without advancing
execution.

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

**How it works:** `workflow_engine.start(workflow_id, input=payload)` runs
the first layer (`enrich-request`) synchronously, then suspends at
`compliance-review` because of `@wait_for_signal`. It returns a
`WorkflowResult` immediately with a `correlation_id` — the caller stores
this ID and polls later. When `deliver_signal()` is called, the workflow
resumes and `settle-transfer` runs to completion.

`WorkflowResult` carries: `workflow_id`, `correlation_id`, `status` (an
`ExecutionStatus` enum), `duration_ms`, `step_results` (dict), and
`variables`. The boolean `result.successful` is `True` when `status` is
`COMPLETED` or `CONFIRMED`.

### The programmatic builder

When you need to construct a workflow dynamically — from a database
configuration or a rules engine — use `WorkflowBuilder`:

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

`WorkflowBuilder.step(step_id, handler, *, depends_on, timeout_ms, max_retries, ...)` accepts a callable and keyword arguments for dependencies, timeouts, and retries. `wait_signal(step_id, signal, *, depends_on, timeout_ms)` inserts a signal-gate step without a real handler — it creates an internal no-op coroutine that the engine replaces with signal-wait logic. `build()` returns a `WorkflowDefinition` you register directly with `WorkflowEngine`.

---

## TCC: Try-Confirm-Cancel

The saga pattern runs steps forward and compensates backwards. **TCC
(Try-Confirm-Cancel)** takes a different approach: all participants first
*tentatively reserve* their resources without committing (Try), then either
all *commit* those reservations (Confirm) or all *release* them (Cancel).
This gives you strong all-or-nothing semantics across participants without
a distributed lock.

TCC suits scenarios where each participant can cheaply hold a reservation
— for example, pre-authorising a payment card hold rather than immediately
charging it.

### The three phases

1. **Try** — every participant reserves resources. Reservations are visible
   internally but not final. If any Try fails, participants that succeeded
   cancel their reservations.
2. **Confirm** — if all Try phases succeed, the coordinator instructs every
   participant to commit its reservation.
3. **Cancel** — if any Try phase fails, the coordinator instructs every
   participant that completed Try to release its reservation.

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

**How it works:** `@tcc_participant(order=1)` tells the TCC engine to run
`WalletParticipant`'s Try phase before `PaymentParticipant`'s — lower
`order` means earlier. **`FromTry()`** is TCC's equivalent of `FromStep`:
it injects the value returned by the same participant's `@try_method`
into its `@confirm_method` and `@cancel_method`.

The engine runs all participants' Try phases in `order` sequence. If every
Try succeeds, it runs all Confirm methods. If any Try fails, it runs Cancel
for every participant that completed its Try — again in declared order. An
`optional=True` participant that fails its Try does not trigger a global
Cancel; its failure is logged and skipped.

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

Use this table to choose between the two approaches:

| Question | Saga | TCC |
|----------|------|-----|
| Steps run independently? | Yes — each commits locally | No — all Try phases must succeed first |
| Needs compensation logic? | Yes, per step | No — Cancel handles rollback |
| Resource reservation needed? | No | Yes — participants hold resources during Try |
| Best for | Long sequential operations | Short all-or-nothing locks |

---

## Persistence: surviving a crash

The engine stores saga and TCC state through the
`TransactionalPersistencePort` protocol. The default adapter keeps state
in memory — fast for development, but lost on process restart. Production
deployments swap in a durable adapter.

### How state flows

Every time a step completes — successfully or otherwise — the engine calls:

1. `persistence_port.update_step_status(correlation_id, step_id, status)` — record the step outcome.
2. `persistence_port.mark_completed(correlation_id, successful)` — record the saga's final result.

On startup, `SagaRecoveryService` queries `persistence_port.get_stale(before)`
to find executions that started but never completed. For each stale saga
still in `IN_FLIGHT` status, it marks the saga `FAILED` and emits lifecycle
events so observability systems can alert on-call.

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

With `recovery_enabled: true`, the framework runs
`SagaRecoveryService.recover_stale()` on a background task every
`recovery_interval_seconds` seconds. Sagas last updated more than
`stale_threshold_seconds` seconds ago are considered stuck, marked failed,
and surfaced for manual investigation or automatic retry.

### Implementing a custom persistence adapter

To persist to a real database, implement `TransactionalPersistencePort` and
register your implementation as a `@bean` or `@component`. The
auto-configuration detects your bean at startup and uses it in preference to
`InMemoryPersistenceAdapter`:

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

When you need to build a saga from dynamic configuration — loading step
definitions from a rules database or a configuration file — `SagaBuilder`
gives you the full fluent API without any decorators:

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

**How it works:** Each `.step(step_id)` call returns a `StepBuilder`. Chain
configuration methods — `.handler()`, `.compensate()`, `.depends_on()`,
`.retry()`, `.backoff_ms()`, `.timeout_ms()`, `.jitter()` — then call
`.add()` to finalise the step and return the parent `SagaBuilder`.
`.build()` runs the same DAG validation as the decorator path: missing
handlers, nonexistent `depends_on` references, and cycles all raise
`SagaValidationError` immediately at registration time.

---

## What you built {.recap}

You began with a concrete problem: a wallet transfer across two independent
aggregates cannot use a single database transaction. You declared
`MoneyTransferSaga` by stacking `@saga` on `@service`, causing the
`OrchestrationBeanPostProcessor` to register it into the auto-configured
`SagaEngine` at startup. Each step uses `Annotated[T, Input()]` and
`Annotated[T, FromStep("step-id")]` marker instances — not bare classes —
for parameter injection; `ctx: SagaContext` is injected by type. The
compensation method `recredit_source` does not receive the saga input; it
pulls the forward step's `DebitResult` via `FromStep("debit-source")`. When
`credit-destination` raises `AggregateNotFound`, the engine automatically
runs `recredit_source`, leaving both balances unchanged.

You explored compensation in depth: five policies ranging from strict
sequential to best-effort parallel, per-step compensation retries and
timeouts, and the non-negotiable requirement that all compensations be
idempotent. You saw how `@workflow(id=...) @service` and `@wait_for_signal`
suspend a long-running workflow until a human delivers a signal, and how
`WorkflowResult.successful` reports the final state. You walked through TCC
as a reservation-based alternative that locks resources across all
participants before committing any. Finally you wired a custom
`TransactionalPersistencePort` and configured `SagaRecoveryService` to
detect and surface stale executions after a crash.

Key concepts to carry forward:

- **`@saga` on `@service`** — the decorator stack that makes a class both a DI bean and a registered saga; `@saga` alone is not enough.
- **Marker instances** — `Input()`, `FromStep("step-id")` must be instances (with parentheses), not bare classes.
- **Compensation via `FromStep`** — compensation methods receive the forward step's result, never the saga input.
- **`SagaEngine.execute(saga_name, input_data)`** — the single call that returns `SagaResult` with `.success`, `.result_of()`, `.failed_steps()`, `.compensated_steps()`.
- **`@workflow(id=...) @service` / `@wait_for_signal`** — signal-driven, long-running alternative; check `WorkflowResult.successful` for the final state.
- **`@tcc` on `@service` / `@tcc_participant`** — reservation-based coordination; `FromTry()` (instance) injects the try-result into confirm and cancel methods.
- **`TransactionalPersistencePort`** — implement and register this protocol to give the engine durable state and crash recovery.

---

## Try it yourself {.exercises}

**Exercise 1 — Parallel balance validation.** Add a `validate-source` step to `MoneyTransferSaga` that checks the source wallet has sufficient funds, without performing the debit. The step should run *in parallel* with nothing (no `depends_on`), and `debit-source` should depend on it. Extend `credit-destination` to depend on `debit-source` as before. Verify the topology via `SagaRegistry.get("money-transfer")` in a test and assert that `definition.steps["debit-source"].depends_on == ["validate-source"]`.

**Exercise 2 — Compensation error handler.** Change `MoneyTransferSaga`'s global compensation policy to `RETRY_WITH_BACKOFF` in `application.yaml`. Then deliberately make `recredit_source` raise `RuntimeError` on the first call and succeed on the second. Write a pytest test using `AsyncMock` on `WalletRepository` that verifies the saga eventually compensates successfully and `result.compensated_steps()` contains `"debit-source"`.

**Exercise 3 — Custom persistence.** Implement `TransactionalPersistencePort` backed by a plain Python `dict` that logs every call. Register it as a `@service` and write a test that runs `TransferService`, then calls `get_state(correlation_id)` on your adapter and asserts the recorded `status` is `"COMPLETED"`. Extend the test to simulate a stale saga by manually setting `status = "IN_FLIGHT"` and a past `started_at`, then assert `SagaRecoveryService.recover_stale(stale_threshold_seconds=0)` returns `1`.
