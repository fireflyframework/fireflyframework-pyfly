# Transactional Engine Guide

PyFly provides a production-ready distributed transaction module built on
hexagonal architecture principles. The `pyfly.transactional` module
implements two complementary patterns -- SAGA orchestration and
Try-Confirm-Cancel (TCC) coordination -- so that you can maintain data
consistency across service boundaries without two-phase commit.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Architecture Overview](#architecture-overview)
3. [SAGA Pattern](#saga-pattern)
   - [The `@saga` Decorator](#the-saga-decorator)
   - [The `@saga_step` Decorator](#the-saga_step-decorator)
   - [Compensation Methods](#compensation-methods)
   - [Step Dependencies (DAG)](#step-dependencies-dag)
   - [Parameter Injection](#parameter-injection)
   - [SagaContext](#sagacontext)
   - [SagaResult and StepOutcome](#sagaresult-and-stepoutcome)
4. [TCC Pattern](#tcc-pattern)
   - [The `@tcc` Decorator](#the-tcc-decorator)
   - [The `@tcc_participant` Decorator](#the-tcc_participant-decorator)
   - [`@try_method`, `@confirm_method`, `@cancel_method`](#try_method-confirm_method-cancel_method)
   - [TccContext and TccResult](#tcccontext-and-tccresult)
   - [TccPhase](#tccphase)
5. [Compensation Policies](#compensation-policies)
6. [Backpressure Strategies](#backpressure-strategies)
7. [Compensation Error Handlers](#compensation-error-handlers)
8. [Programmatic Saga Definition](#programmatic-saga-definition)
9. [Saga Composition](#saga-composition)
10. [Persistence](#persistence)
11. [Observability](#observability)
12. [Configuration Reference](#configuration-reference)
13. [Auto-Configuration](#auto-configuration)
14. [Complete Example: Order Fulfillment](#complete-example-order-fulfillment)
15. [Testing](#testing)
16. [Java Comparison](#java-comparison)

---

## Introduction

In a microservices architecture, business operations frequently span multiple
services. A single "place order" action might reserve inventory, charge a
payment, and schedule shipping -- each managed by a different service with its
own database. Traditional ACID transactions cannot span these boundaries, so
you need a pattern that coordinates eventual consistency and provides
automatic rollback when something goes wrong.

PyFly's transactional engine provides two complementary patterns:

* **SAGA** -- an orchestration-based pattern where a central coordinator
  executes a directed acyclic graph (DAG) of steps and automatically runs
  compensating actions in reverse when any step fails.
* **TCC (Try-Confirm-Cancel)** -- a reservation-based pattern where all
  participants tentatively reserve resources (Try), then either commit
  (Confirm) or roll back (Cancel) in lockstep.

Both patterns are fully async, integrate with PyFly's DI container, and
expose Protocol-based ports for persistence, observability, backpressure, and
error handling.

---

## Architecture Overview

```
pyfly.transactional
+----------------------------------------------------------------------+
|                                                                      |
|  +-------------------+      +-------------------+                    |
|  |    SAGA Engine    |      |    TCC Engine     |                    |
|  +--------+----------+      +--------+----------+                    |
|           |                          |                               |
|  +--------v----------+      +--------v----------+                    |
|  | ExecutionOrch.    |      | ExecutionOrch.    |                    |
|  | (DAG scheduler)   |      | (3-phase coord.)  |                    |
|  +--------+----------+      +--------+----------+                    |
|           |                          |                               |
|  +--------v----------+      +--------v----------+                    |
|  | StepInvoker       |      | ParticipantInvoker|                    |
|  | ArgumentResolver  |      | TccArgResolver    |                    |
|  +--------+----------+      +--------+----------+                    |
|           |                          |                               |
|  +--------v----------+      +--------v----------+                    |
|  | SagaCompensator   |      |   (Cancel phase)  |                    |
|  | (5 policies)      |      |                   |                    |
|  +-------------------+      +-------------------+                    |
|                                                                      |
|  Shared Infrastructure                                               |
|  +-------------------------------+  +----------------------------+   |
|  | Ports (Protocols)             |  | Adapters                   |   |
|  |  TransactionalPersistencePort |  |  InMemoryPersistenceAdapter|   |
|  |  TransactionalEventsPort      |  |  LoggerEventsAdapter       |   |
|  |  BackpressureStrategyPort     |  |  CompositeEventsAdapter    |   |
|  |  CompensationErrorHandlerPort |  |  Adaptive / Batched / CB   |   |
|  +-------------------------------+  +----------------------------+   |
|                                                                      |
|  Registries                                                          |
|  +-------------------------------+  +----------------------------+   |
|  | SagaRegistry                  |  | TccRegistry               |   |
|  |  discovers @saga beans        |  |  discovers @tcc beans      |   |
|  |  builds SagaDefinition        |  |  builds TccDefinition      |   |
|  |  validates DAG                |  |  validates participants    |   |
|  +-------------------------------+  +----------------------------+   |
|                                                                      |
+----------------------------------------------------------------------+
```

The module follows hexagonal architecture. Four `@runtime_checkable Protocol`
ports define the boundary between the engine and its infrastructure.
Adapters (in-memory persistence, logger events, etc.) ship as defaults and
can be replaced by production implementations for databases, message
brokers, or metrics systems.

---

## SAGA Pattern

### The `@saga` Decorator

`@saga` marks a class as a saga definition. The saga registry discovers
these classes from the DI container at startup.

```python
from pyfly.transactional.saga.annotations import saga
from pyfly.container import component

@saga(name="create-order", layer_concurrency=5)
@component
class CreateOrderSaga:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Unique saga name used for lookup and logging. |
| `layer_concurrency` | `int` | `0` | Max concurrent steps per dependency layer. `0` = unlimited. |

The decorator sets `__pyfly_saga__` metadata on the class, which the
`SagaRegistry` reads during bean scanning.

### The `@saga_step` Decorator

`@saga_step` marks an async method as a step in the saga. Steps form a DAG
via `depends_on` and execute in topological-layer order.

```python
from pyfly.transactional.saga.annotations import saga_step, Input, FromStep
from pyfly.transactional.saga.core.context import SagaContext
from typing import Annotated

@saga_step(
    id="reserve-inventory",
    compensate="release_inventory",
    depends_on=[],
    retry=3,
    backoff_ms=100,
    timeout_ms=5000,
    jitter=True,
    jitter_factor=0.5,
)
async def reserve_inventory(
    self,
    request: Annotated[OrderRequest, Input],
    ctx: SagaContext,
) -> ReservationResult:
    return await self.inventory_service.reserve(request.items)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `str` | *required* | Unique step identifier within the saga. |
| `compensate` | `str \| None` | `None` | Name of the compensation method on the saga class. |
| `depends_on` | `list[str] \| None` | `[]` | Step ids this step must wait for. |
| `retry` | `int` | `0` | Number of retry attempts on failure. |
| `backoff_ms` | `int` | `0` | Base backoff duration in milliseconds. |
| `timeout_ms` | `int` | `0` | Execution timeout in milliseconds (0 = no timeout). |
| `jitter` | `bool` | `False` | Whether to add jitter to backoff. |
| `jitter_factor` | `float` | `0.0` | Fraction of backoff used as jitter range. |
| `cpu_bound` | `bool` | `False` | Offload to a thread/process pool. |
| `idempotency_key` | `str \| None` | `None` | Template string for deduplication. |
| `compensation_retry` | `int \| None` | `None` | Override retry count for the compensation action. |
| `compensation_backoff_ms` | `int \| None` | `None` | Override backoff for the compensation action. |
| `compensation_timeout_ms` | `int \| None` | `None` | Override timeout for the compensation action. |
| `compensation_critical` | `bool` | `False` | If `True`, saga failure is raised when compensation fails. |

### Compensation Methods

When a saga step fails, the engine compensates all previously completed
steps in reverse order. Each step declares its compensation method by name
via the `compensate` parameter.

```python
@saga(name="create-order")
@component
class CreateOrderSaga:

    @saga_step(id="reserve-inventory", compensate="release_inventory")
    async def reserve_inventory(
        self,
        request: Annotated[OrderRequest, Input],
    ) -> ReservationResult:
        return await self.inventory_service.reserve(request.items)

    async def release_inventory(
        self,
        result: Annotated[ReservationResult, FromStep("reserve-inventory")],
    ) -> None:
        await self.inventory_service.release(result)
```

Compensation methods receive their parameters through the same injection
system as forward steps. You can inject the original step's result via
`FromStep`, the triggering error via `CompensationError`, compensation
results from other steps via `FromCompensationResult`, and so on.

#### External Compensation Steps

For compensation logic that lives outside the saga class, use
`@compensation_step`:

```python
from pyfly.transactional.saga.annotations import compensation_step

@compensation_step(saga="create-order", for_step_id="reserve-inventory")
@component
class ReleaseInventoryCompensation:
    async def execute(
        self,
        result: Annotated[ReservationResult, FromStep("reserve-inventory")],
    ) -> None:
        await self.inventory_service.release(result)
```

#### External Steps

For forward-step logic that lives outside the saga class, use
`@external_step`:

```python
from pyfly.transactional.saga.annotations import external_step

@external_step(
    saga="create-order",
    id="notify-warehouse",
    depends_on=["reserve-inventory"],
    retry=2,
    backoff_ms=500,
)
@component
class NotifyWarehouseStep:
    async def execute(
        self,
        reservation: Annotated[ReservationResult, FromStep("reserve-inventory")],
    ) -> None:
        await self.notification_service.notify(reservation)
```

### Step Dependencies (DAG)

Steps declare dependencies through `depends_on`, forming a directed acyclic
graph. The engine computes topology layers -- groups of steps whose
dependencies are all satisfied -- and executes each layer in parallel via
`asyncio.gather`.

```
Layer 0:  [validate-order]
              |
Layer 1:  [reserve-inventory]   [check-fraud]
              |                      |
Layer 2:  [process-payment] --------+
              |
Layer 3:  [ship-order]
```

Steps within a layer run concurrently, bounded by the saga's
`layer_concurrency` setting (enforced via `asyncio.Semaphore`). The
registry validates the DAG at startup using Kahn's algorithm and raises
`SagaValidationError` if:

* A `depends_on` entry references a nonexistent step.
* The dependency graph contains a cycle.

### Parameter Injection

Saga step and compensation methods declare their parameters using
`typing.Annotated` with marker classes. The `ArgumentResolver` inspects
type hints at runtime via `typing.get_type_hints(func, include_extras=True)`
and resolves each parameter from the saga context.

| Marker | Usage | Description |
|--------|-------|-------------|
| `Input` | `Annotated[T, Input]` | Inject the entire input payload. |
| `Input("key")` | `Annotated[T, Input("key")]` | Inject a specific key from the input. |
| `FromStep("id")` | `Annotated[T, FromStep("id")]` | Inject a previous step's result. |
| `Header("name")` | `Annotated[str, Header("name")]` | Inject a single header value. |
| `Headers` | `Annotated[dict, Headers]` | Inject the full headers mapping. |
| `Variable("name")` | `Annotated[T, Variable("name")]` | Inject a saga-scoped variable (read). |
| `Variables` | `Annotated[dict, Variables]` | Inject the full variables mapping. |
| `SetVariable("key")` | `Annotated[T, SetVariable("key")]` | Write the return value into a variable. |
| `FromCompensationResult("id")` | `Annotated[T, FromCompensationResult("id")]` | Inject a compensation result. |
| `CompensationError` | `Annotated[Exception, CompensationError]` | Inject the error that triggered compensation. |
| `SagaContext` | `ctx: SagaContext` | Inject the execution context (by type, no annotation needed). |

Example combining multiple markers:

```python
@saga_step(id="process-payment", depends_on=["reserve-inventory"])
async def process_payment(
    self,
    request: Annotated[OrderRequest, Input],
    reservation: Annotated[ReservationResult, FromStep("reserve-inventory")],
    user_id: Annotated[str, Header("X-User-Id")],
    ctx: SagaContext,
) -> PaymentResult:
    return await self.payment_service.charge(
        user_id=user_id,
        amount=request.total,
        reservation_id=reservation.id,
    )
```

### SagaContext

`SagaContext` is the mutable runtime state carrier threaded through every
step of a saga execution. It tracks step results, statuses, timing, and
provides helper methods for reading and writing shared state.

```python
from pyfly.transactional.saga.core.context import SagaContext
```

| Field | Type | Description |
|-------|------|-------------|
| `correlation_id` | `str` | Auto-generated UUID identifying this execution. |
| `saga_name` | `str` | Name of the saga being executed. |
| `headers` | `dict[str, str]` | Request/message headers. |
| `variables` | `dict[str, Any]` | Saga-scoped variables. |
| `step_results` | `dict[str, Any]` | Results keyed by step id. |
| `step_statuses` | `dict[str, StepStatus]` | Current status of each step. |
| `step_attempts` | `dict[str, int]` | Retry attempt count per step. |
| `step_latencies_ms` | `dict[str, float]` | Execution latency per step. |
| `step_started_at` | `dict[str, Any]` | Start timestamp per step. |
| `compensation_results` | `dict[str, Any]` | Results of compensation actions. |
| `compensation_errors` | `dict[str, Exception]` | Errors during compensation. |
| `idempotency_keys` | `set[str]` | Deduplication keys seen so far. |
| `topology_layers` | `list[list[str]]` | Computed topology layers. |
| `step_dependencies` | `dict[str, list[str]]` | Step dependency graph. |

#### Helper Methods

```python
# Results
ctx.get_result("reserve-inventory")       # Any | None
ctx.set_result("reserve-inventory", data)

# Variables
ctx.get_variable("retry_count")           # Any | None
ctx.set_variable("retry_count", 3)

# Status
ctx.set_step_status("reserve-inventory", StepStatus.DONE)

# Idempotency
ctx.has_idempotency_key("order-123-reserve")   # bool
ctx.add_idempotency_key("order-123-reserve")
```

### SagaResult and StepOutcome

After a saga completes (successfully or via compensation), the engine
produces an immutable `SagaResult` containing a `StepOutcome` for every
step.

```python
from pyfly.transactional.saga.core.result import SagaResult, StepOutcome
```

#### StepOutcome

```python
@dataclass(frozen=True)
class StepOutcome:
    status: StepStatus
    attempts: int
    latency_ms: float
    result: Any
    error: Exception | None
    compensated: bool
    started_at: datetime
    compensation_result: Any | None
    compensation_error: Exception | None
```

#### SagaResult

```python
@dataclass(frozen=True)
class SagaResult:
    saga_name: str
    correlation_id: str
    started_at: datetime
    completed_at: datetime
    success: bool
    error: Exception | None
    headers: dict[str, str]
    steps: dict[str, StepOutcome]
```

| Method | Return Type | Description |
|--------|-------------|-------------|
| `result_of(step_id)` | `Any \| None` | Return the result of a specific step. |
| `failed_steps()` | `dict[str, StepOutcome]` | All steps with status `FAILED`. |
| `compensated_steps()` | `dict[str, StepOutcome]` | All steps with status `COMPENSATED`. |

Usage:

```python
result: SagaResult = await saga_engine.execute("create-order", order_request)

if result.success:
    reservation = result.result_of("reserve-inventory")
    print(f"Order placed, reservation: {reservation}")
else:
    for step_id, outcome in result.failed_steps().items():
        print(f"Step '{step_id}' failed: {outcome.error}")
```

---

## TCC Pattern

TCC (Try-Confirm-Cancel) is a reservation-based distributed transaction
pattern that proceeds in three phases:

1. **Try** -- tentatively reserve resources across all participants.
2. **Confirm** -- commit all reservations (all Try phases succeeded).
3. **Cancel** -- release all reservations (any Try phase failed).

### The `@tcc` Decorator

`@tcc` marks a class as a TCC transaction definition.

```python
from pyfly.transactional.tcc.annotations import tcc
from pyfly.container import component

@tcc(
    name="order-payment",
    timeout_ms=30000,
    retry_enabled=True,
    max_retries=3,
    backoff_ms=1000,
)
@component
class OrderPaymentTcc:
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Unique TCC transaction name. |
| `timeout_ms` | `int` | `0` | Global timeout in milliseconds (0 = no timeout). |
| `retry_enabled` | `bool` | `False` | Whether retries are enabled. |
| `max_retries` | `int` | `0` | Maximum retry attempts. |
| `backoff_ms` | `int` | `0` | Base backoff duration in milliseconds. |

### The `@tcc_participant` Decorator

`@tcc_participant` marks a nested class as a participant in the TCC
transaction. Participants are ordered by their `order` parameter.

```python
from pyfly.transactional.tcc.annotations import tcc_participant

@tcc(name="order-payment")
@component
class OrderPaymentTcc:

    @tcc_participant(id="payment-service", order=1, timeout_ms=5000)
    class PaymentParticipant:
        ...

    @tcc_participant(id="loyalty-service", order=2, optional=True)
    class LoyaltyParticipant:
        ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `str` | *required* | Unique participant identifier. |
| `order` | `int` | `0` | Execution order (lower values execute first). |
| `timeout_ms` | `int` | `0` | Participant-level timeout in milliseconds (0 = no timeout). |
| `optional` | `bool` | `False` | Whether the participant is optional (failure does not trigger cancel). |

### `@try_method`, `@confirm_method`, `@cancel_method`

Each participant must implement exactly three phase methods. Each method
decorator accepts the same parameters:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `timeout_ms` | `int` | `0` | Execution timeout in milliseconds. |
| `retry` | `int` | `0` | Retry attempts on failure. |
| `backoff_ms` | `int` | `0` | Base backoff duration in milliseconds. |

Complete example:

```python
from pyfly.transactional.tcc.annotations import (
    tcc, tcc_participant, try_method, confirm_method, cancel_method, FromTry,
)
from pyfly.transactional.tcc.core.context import TccContext
from typing import Annotated

@tcc(name="order-payment", timeout_ms=30000)
@component
class OrderPaymentTcc:

    @tcc_participant(id="payment-service", order=1)
    class PaymentParticipant:

        @try_method(timeout_ms=5000, retry=2, backoff_ms=100)
        async def try_reserve(
            self,
            request: Annotated[PaymentRequest, Input],
            ctx: TccContext,
        ) -> ReservationId:
            return await self.payment_service.reserve(request)

        @confirm_method(timeout_ms=10000, retry=3)
        async def confirm(
            self,
            reservation_id: Annotated[ReservationId, FromTry()],
            ctx: TccContext,
        ) -> None:
            await self.payment_service.commit(reservation_id)

        @cancel_method(timeout_ms=5000, retry=1)
        async def cancel(
            self,
            reservation_id: Annotated[ReservationId, FromTry()],
        ) -> None:
            await self.payment_service.release(reservation_id)
```

The `FromTry` marker injects the result of the participant's own try method
into confirm and cancel methods.

### TccContext and TccResult

#### TccContext

`TccContext` is the mutable runtime state carrier for a TCC execution,
analogous to `SagaContext`.

```python
from pyfly.transactional.tcc.core.context import TccContext
```

| Field | Type | Description |
|-------|------|-------------|
| `correlation_id` | `str` | Auto-generated UUID identifying this execution. |
| `tcc_name` | `str` | Name of the TCC transaction. |
| `headers` | `dict[str, str]` | Request/message headers. |
| `variables` | `dict[str, Any]` | Transaction-scoped variables. |
| `try_results` | `dict[str, Any]` | Try-phase results keyed by participant id. |
| `current_phase` | `TccPhase` | Current phase of the execution. |
| `participant_statuses` | `dict[str, TccPhase]` | Phase reached by each participant. |

Helper methods:

```python
ctx.get_try_result("payment-service")          # Any | None
ctx.set_try_result("payment-service", result)
ctx.set_phase(TccPhase.CONFIRM)
ctx.set_participant_status("payment-service", TccPhase.CONFIRM)
```

#### TccResult

After a TCC transaction completes, the engine produces an immutable
`TccResult` containing a `ParticipantResult` for every participant.

```python
from pyfly.transactional.tcc.core.result import TccResult, ParticipantResult
```

**ParticipantResult**:

| Field | Type | Description |
|-------|------|-------------|
| `participant_id` | `str` | Unique participant identifier. |
| `try_result` | `Any` | Value returned by the try phase. |
| `try_error` | `Exception \| None` | Error from the try phase. |
| `confirm_error` | `Exception \| None` | Error from the confirm phase. |
| `cancel_error` | `Exception \| None` | Error from the cancel phase. |
| `final_phase` | `TccPhase` | Last phase reached. |
| `latency_ms` | `float` | Wall-clock duration in milliseconds. |

**TccResult**:

| Field | Type | Description |
|-------|------|-------------|
| `correlation_id` | `str` | Execution identifier. |
| `tcc_name` | `str` | TCC transaction name. |
| `success` | `bool` | Whether the transaction committed. |
| `final_phase` | `TccPhase` | Final phase of the overall execution. |
| `try_results` | `dict[str, Any]` | Try results by participant id. |
| `participant_results` | `dict[str, ParticipantResult]` | Full results per participant. |
| `started_at` | `datetime` | When the execution started. |
| `completed_at` | `datetime` | When the execution finished. |
| `error` | `Exception \| None` | Root-cause error, if any. |
| `failed_participant_id` | `str \| None` | Id of the participant that caused failure. |

| Method | Return Type | Description |
|--------|-------------|-------------|
| `result_of(participant_id)` | `Any \| None` | Return the try-result of a participant. |
| `failed_participants()` | `dict[str, ParticipantResult]` | All participants that encountered errors. |

### TccPhase

```python
from pyfly.transactional.tcc.core.phase import TccPhase

class TccPhase(str, Enum):
    TRY = "TRY"
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
```

The TCC engine transitions through these phases:
1. Execute all participants' `@try_method` in order.
2. If all succeed, execute all `@confirm_method` in order.
3. If any try fails, execute `@cancel_method` for all participants that
   completed their try phase.

---

## Compensation Policies

The `SagaCompensator` supports five policies that control how compensating
transactions are executed when a saga step fails. Set the policy globally
via configuration or per-composition via the builder.

```python
from pyfly.transactional.shared.types import CompensationPolicy
```

### STRICT_SEQUENTIAL

Compensates in reverse completion order, one step at a time. Stops
immediately on the first compensation error.

```
Step C completed --> compensate C
Step B completed --> compensate B
Step A completed --> compensate A (if B succeeded)
```

**Use when**: Compensation ordering matters and partial rollback is
unacceptable.

### GROUPED_PARALLEL

Reverses the topology layers and compensates all steps within each layer
in parallel via `asyncio.gather`. Layers are processed sequentially.

```
Layer 2: [payment, shipping] --> compensate both in parallel
Layer 1: [inventory]         --> compensate sequentially
Layer 0: [validation]        --> compensate sequentially
```

**Use when**: You want speed without violating the dependency structure.

### RETRY_WITH_BACKOFF

Compensates in reverse order with exponential backoff retries. Each step
uses its `compensation_retry` and `compensation_backoff_ms` settings (or
defaults of 3 retries and 1000ms backoff). If all retries are exhausted, the
error is delegated to the `CompensationErrorHandlerPort` and re-raised.

**Use when**: Compensations are likely to succeed on retry (e.g. transient
network failures).

### CIRCUIT_BREAKER

Compensates in reverse order but tracks consecutive failures. After 3
consecutive compensation failures, the circuit "opens" and remaining
compensations are skipped with a warning log.

**Use when**: You want to avoid cascading failures during compensation. A
manual recovery process handles the skipped compensations.

### BEST_EFFORT_PARALLEL

Runs all compensations simultaneously via `asyncio.gather(return_exceptions=True)`.
Errors are logged and reported to the `CompensationErrorHandlerPort` but
never raised -- the saga completes even if some compensations fail.

**Use when**: Speed is critical and you have separate reconciliation
processes to handle partial compensation failures.

---

## Backpressure Strategies

Three implementations of `BackpressureStrategyPort` control how the engine
processes batches of items under load.

```python
from pyfly.transactional.shared.engine.backpressure import (
    AdaptiveBackpressureStrategy,
    BatchedBackpressureStrategy,
    CircuitBreakerBackpressureStrategy,
    BackpressureStrategyFactory,
)
from pyfly.transactional.shared.types import BackpressureConfig
```

### Adaptive

Dynamically adjusts concurrency based on runtime feedback. Starts at the
configured concurrency level and:

* **Increases** concurrency when average latency is low and no errors occur.
* **Decreases** concurrency on errors or high latency.

Uses `asyncio.Semaphore` internally.

### Batched

Processes items in fixed-size batches. Each batch runs in parallel via
`asyncio.gather`, and the strategy waits for the current batch to finish
before starting the next.

### CircuitBreaker

Tracks consecutive failures across items. Transitions through three states:

* **CLOSED** -- normal processing.
* **OPEN** -- items are immediately rejected after hitting the failure
  threshold.
* **HALF_OPEN** -- after `wait_duration_ms`, a single probe is allowed. On
  success the circuit closes; on failure it re-opens.

### Configuration

```python
config = BackpressureConfig(
    strategy="adaptive",     # "adaptive" | "batched" | "circuit_breaker"
    concurrency=10,          # Max parallel tasks (adaptive)
    batch_size=5,            # Items per batch (batched)
    failure_threshold=50,    # Failures before opening circuit
    success_threshold=2,     # Successes before closing circuit
    wait_duration_ms=60000,  # Wait before half-open probe
)
```

### Factory

```python
strategy = BackpressureStrategyFactory.create("adaptive")
results = await strategy.apply(items, processor_fn, config)
```

---

## Compensation Error Handlers

Four implementations of `CompensationErrorHandlerPort` handle errors that
occur during compensation itself.

```python
from pyfly.transactional.shared.engine.compensation import (
    FailFastErrorHandler,
    LogAndContinueErrorHandler,
    RetryWithBackoffErrorHandler,
    CompositeCompensationErrorHandler,
    CompensationErrorHandlerFactory,
)
```

### FailFast

Re-raises the compensation error immediately. No further compensations are
attempted.

```python
handler = FailFastErrorHandler()
```

### LogAndContinue

Logs the error at `ERROR` level and returns normally, allowing the next
compensation to proceed.

```python
handler = LogAndContinueErrorHandler()
```

### RetryWithBackoff

Retries the failed compensation with exponential backoff. If a
`compensation_fn` callable is present in the context, it is re-invoked on
each retry. After exhausting all retries, re-raises the error.

```python
handler = RetryWithBackoffErrorHandler(
    max_retries=3,
    backoff_ms=1000,
    backoff_multiplier=2.0,
)
```

### Composite

Chains a primary handler with a fallback. If the primary handler raises,
the fallback receives the original error.

```python
handler = CompositeCompensationErrorHandler(
    primary=RetryWithBackoffErrorHandler(max_retries=2),
    fallback=LogAndContinueErrorHandler(),
)
```

### Factory

```python
handler = CompensationErrorHandlerFactory.create("retry_with_backoff", max_retries=3)
```

Available types: `"fail_fast"`, `"log_and_continue"`, `"retry_with_backoff"`.

---

## Programmatic Saga Definition

When the decorator syntax is not appropriate -- for example when sagas are
loaded from configuration or created dynamically -- use the `SagaBuilder`
fluent API.

```python
from pyfly.transactional.saga.registry.saga_builder import SagaBuilder

saga_def = (
    SagaBuilder("order-saga")
    .step("validate")
        .handler(validate_fn)
        .add()
    .step("reserve")
        .handler(reserve_fn)
        .compensate(release_fn)
        .depends_on("validate")
        .retry(3)
        .backoff_ms(100)
        .timeout_ms(5000)
        .jitter(enabled=True, factor=0.5)
        .add()
    .step("payment")
        .handler(payment_fn)
        .compensate(refund_fn)
        .depends_on("reserve")
        .add()
    .layer_concurrency(5)
    .build()
)
```

### SagaBuilder API

| Method | Description |
|--------|-------------|
| `SagaBuilder(name)` | Create a new builder for a named saga. |
| `.step(step_id)` | Begin configuring a new step (returns `StepBuilder`). |
| `.layer_concurrency(n)` | Set max steps per layer (0 = unlimited). |
| `.build()` | Validate the DAG and produce an immutable `SagaDefinition`. |

### StepBuilder API

| Method | Description |
|--------|-------------|
| `.handler(func)` | Set the forward-action handler. |
| `.compensate(func)` | Set the compensation handler. |
| `.depends_on(*step_ids)` | Declare dependencies on preceding steps. |
| `.retry(count)` | Set max retry attempts. |
| `.backoff_ms(ms)` | Set base backoff duration. |
| `.timeout_ms(ms)` | Set execution timeout. |
| `.jitter(enabled, factor)` | Enable jitter on backoff. |
| `.cpu_bound(enabled)` | Mark step as CPU-bound. |
| `.add()` | Finalise the step and return the parent `SagaBuilder`. |

### Validation

The `build()` method performs full validation:

1. At least one step must exist.
2. Every step must have a handler.
3. All `depends_on` references must point to existing step ids.
4. The dependency graph must be acyclic (verified via Kahn's algorithm).

On failure, `SagaValidationError` is raised with a descriptive message.

---

## Saga Composition

For business operations that span multiple independent sagas, the
`SagaCompositionBuilder` constructs a DAG of sagas with data flow wiring
between them.

```python
from pyfly.transactional.saga.composition.composition_builder import SagaCompositionBuilder
from pyfly.transactional.shared.types import CompensationPolicy

composition = (
    SagaCompositionBuilder("order-fulfillment")
    .saga("reserve-inventory")
        .depends_on()
        .add()
    .saga("process-payment")
        .depends_on("reserve-inventory")
        .data_flow(
            source_saga="reserve-inventory",
            source_step="reserve-items",
            target_key="reservation",
        )
        .add()
    .saga("ship-order")
        .depends_on("process-payment")
        .data_flow(
            source_saga="process-payment",
            source_step="charge-card",
            target_key="payment_confirmation",
        )
        .add()
    .compensation_policy(CompensationPolicy.GROUPED_PARALLEL)
    .build()
)
```

### SagaCompositionBuilder API

| Method | Description |
|--------|-------------|
| `SagaCompositionBuilder(name)` | Create a new composition builder. |
| `.saga(saga_name)` | Begin defining an entry for the named saga (returns `_EntryBuilder`). |
| `.compensation_policy(policy)` | Set the compensation policy for the composition. |
| `.build()` | Validate and produce an immutable `SagaComposition`. |

### _EntryBuilder API

| Method | Description |
|--------|-------------|
| `.depends_on(*saga_names)` | Declare saga-level dependencies. |
| `.data_flow(source_saga, source_step, target_key)` | Wire output from an upstream saga. |
| `.add()` | Finalise the entry and return to the composition builder. |

### Data Flow Types

```python
from pyfly.transactional.saga.composition.composition import SagaDataFlow

@dataclass(frozen=True)
class SagaDataFlow:
    source_saga: str           # Name of the source saga
    source_step: str | None    # Specific step (None = entire SagaResult)
    target_key: str | None     # Input key for the target saga (None = merge)
```

### Execution

Execute a composition through the `SagaCompositor`:

```python
result = await compositor.execute(composition, initial_inputs)
```

The compositor executes sagas in topological order, resolves data flows
between them, and applies the configured compensation policy if any saga
fails.

---

## Persistence

### TransactionalPersistencePort

The persistence port defines the contract for storing and querying
transactional execution state.

```python
from pyfly.transactional.shared.ports.outbound import TransactionalPersistencePort
```

| Method | Description |
|--------|-------------|
| `persist_state(state)` | Persist the initial state of a transactional context. |
| `get_state(correlation_id)` | Retrieve persisted state, or `None` if absent. |
| `update_step_status(correlation_id, step_id, status)` | Update a step's status. |
| `mark_completed(correlation_id, successful)` | Mark transaction as completed or failed. |
| `get_in_flight()` | Return all in-flight transactions. |
| `get_stale(before)` | Return transactions older than a given timestamp. |
| `cleanup(older_than)` | Delete old completed records. Returns count. |
| `is_healthy()` | Health check for the storage backend. |

### InMemoryPersistenceAdapter

The default adapter stores all state in a Python `dict`. All state is lost
on process restart.

```python
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter

adapter = InMemoryPersistenceAdapter()
await adapter.persist_state({
    "correlation_id": "abc-123",
    "saga_name": "create-order",
})
```

State entries follow this structure:

```python
{
    "correlation_id": "abc-123",
    "status": "IN_FLIGHT",           # IN_FLIGHT | COMPLETED | FAILED
    "started_at": datetime,
    "completed_at": datetime | None,
    "successful": bool | None,
    "steps": {
        "step-id": {"status": "DONE"},
    },
}
```

### Recovery

The `SagaRecoveryService` detects and recovers stale in-flight sagas:

```python
from pyfly.transactional.saga.persistence.recovery import SagaRecoveryService

recovery = SagaRecoveryService(
    persistence_port=persistence_adapter,
    saga_engine=saga_engine,
    events_port=events_adapter,
)

# Recover sagas stuck for more than 10 minutes
recovered_count = await recovery.recover_stale(stale_threshold_seconds=600)

# Clean up completed sagas older than 24 hours
cleaned_count = await recovery.cleanup(older_than_hours=24)
```

The recovery algorithm:

1. Calculate a UTC cutoff from `now() - stale_threshold_seconds`.
2. Query persistence for all sagas whose last update is older than cutoff.
3. For each stale saga still in `IN_FLIGHT` status, mark it as `FAILED`.
4. Emit lifecycle events for each recovered saga.

---

## Observability

### TransactionalEventsPort

The events port defines the contract for emitting lifecycle events.

```python
from pyfly.transactional.shared.ports.outbound import TransactionalEventsPort
```

| Method | When Fired |
|--------|------------|
| `on_start(name, correlation_id)` | Transaction begins execution. |
| `on_step_success(name, correlation_id, step_id, attempts, latency_ms)` | Step completes successfully. |
| `on_step_failed(name, correlation_id, step_id, error, attempts, latency_ms)` | Step fails after all retries. |
| `on_compensated(name, correlation_id, step_id, error)` | Compensation executed. `error=None` on success. |
| `on_completed(name, correlation_id, success)` | Transaction finishes (committed or compensated). |

### LoggerEventsAdapter

Writes structured log messages for every lifecycle event. Uses the
`pyfly.transactional.events` logger. Successful operations log at
`INFO`; failures log at `WARNING`.

```python
from pyfly.transactional.shared.observability.events import LoggerEventsAdapter

adapter = LoggerEventsAdapter()
```

### CompositeEventsAdapter

Broadcasts events to multiple adapters. If one adapter fails, the error is
logged and remaining adapters still receive the event.

```python
from pyfly.transactional.shared.observability.events import CompositeEventsAdapter

composite = CompositeEventsAdapter(
    LoggerEventsAdapter(),
    metrics_adapter,
    eda_adapter,
)
```

This is how auto-configuration wires multiple observability sinks -- Logger
is always present, and EDA/metrics adapters are added when their respective
modules are available.

---

## Configuration Reference

```yaml
pyfly:
  transactional:
    enabled: true

    saga:
      enabled: true
      compensation_policy: STRICT_SEQUENTIAL
      default_timeout_ms: 300000
      max_concurrent_sagas: 100
      layer_concurrency: 0
      persistence_enabled: true
      metrics_enabled: true
      recovery_enabled: true
      recovery_interval_seconds: 60
      stale_threshold_seconds: 600
      cleanup_older_than_hours: 24

    tcc:
      enabled: true
      default_timeout_ms: 30000
      retry_enabled: true
      max_retries: 3
      backoff_ms: 1000
      persistence_enabled: true
      metrics_enabled: true

    backpressure:
      strategy: adaptive
      concurrency: 10
      batch_size: 5
      failure_threshold: 50
      success_threshold: 2
      wait_duration_ms: 60000
```

### Saga Properties

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.transactional.saga.enabled` | `bool` | `true` | Enable the saga engine. |
| `pyfly.transactional.saga.compensation_policy` | `str` | `STRICT_SEQUENTIAL` | Default compensation policy. |
| `pyfly.transactional.saga.default_timeout_ms` | `int` | `300000` | Default step timeout (5 minutes). |
| `pyfly.transactional.saga.max_concurrent_sagas` | `int` | `100` | Max concurrent saga executions. |
| `pyfly.transactional.saga.layer_concurrency` | `int` | `0` | Default max steps per layer (0 = unlimited). |
| `pyfly.transactional.saga.persistence_enabled` | `bool` | `true` | Enable state persistence. |
| `pyfly.transactional.saga.metrics_enabled` | `bool` | `true` | Enable metrics collection. |
| `pyfly.transactional.saga.recovery_enabled` | `bool` | `true` | Enable automatic recovery of stale sagas. |
| `pyfly.transactional.saga.recovery_interval_seconds` | `int` | `60` | How often recovery runs (seconds). |
| `pyfly.transactional.saga.stale_threshold_seconds` | `int` | `600` | Sagas older than this are considered stale. |
| `pyfly.transactional.saga.cleanup_older_than_hours` | `int` | `24` | Clean up completed sagas older than this. |

### TCC Properties

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.transactional.tcc.enabled` | `bool` | `true` | Enable the TCC engine. |
| `pyfly.transactional.tcc.default_timeout_ms` | `int` | `30000` | Default transaction timeout. |
| `pyfly.transactional.tcc.retry_enabled` | `bool` | `true` | Enable retries. |
| `pyfly.transactional.tcc.max_retries` | `int` | `3` | Max retry attempts. |
| `pyfly.transactional.tcc.backoff_ms` | `int` | `1000` | Base backoff in milliseconds. |
| `pyfly.transactional.tcc.persistence_enabled` | `bool` | `true` | Enable state persistence. |
| `pyfly.transactional.tcc.metrics_enabled` | `bool` | `true` | Enable metrics collection. |

### Backpressure Properties

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.transactional.backpressure.strategy` | `str` | `adaptive` | Strategy type: `adaptive`, `batched`, `circuit_breaker`. |
| `pyfly.transactional.backpressure.concurrency` | `int` | `10` | Max concurrent tasks (adaptive strategy). |
| `pyfly.transactional.backpressure.batch_size` | `int` | `5` | Items per batch (batched strategy). |
| `pyfly.transactional.backpressure.failure_threshold` | `int` | `50` | Failures before circuit opens. |
| `pyfly.transactional.backpressure.success_threshold` | `int` | `2` | Successes before circuit closes. |
| `pyfly.transactional.backpressure.wait_duration_ms` | `int` | `60000` | Wait before half-open probe. |

`SagaEngineProperties` is bound via
`@config_properties(prefix="pyfly.transactional.saga")`. `TccEngineProperties`
and `BackpressureProperties` are plain dataclasses populated directly by
auto-configuration.

---

## Auto-Configuration

`TransactionalEngineAutoConfiguration` activates when
`pyfly.transactional.enabled=true` and wires the following beans into the
DI container:

| Bean | Type | Description |
|------|------|-------------|
| `saga_engine_properties` | `SagaEngineProperties` | Saga configuration. |
| `tcc_engine_properties` | `TccEngineProperties` | TCC configuration. |
| `backpressure_properties` | `BackpressureProperties` | Backpressure configuration. |
| `in_memory_persistence_adapter` | `InMemoryPersistenceAdapter` | Default in-memory persistence. |
| `logger_events_adapter` | `LoggerEventsAdapter` | Default logging events adapter. |
| `saga_argument_resolver` | `ArgumentResolver` | Parameter injection resolver. |
| `saga_step_invoker` | `StepInvoker` | Saga step and compensation invoker. |
| `saga_compensator` | `SagaCompensator` | Compensation executor (5 policies). |
| `saga_execution_orchestrator` | `SagaExecutionOrchestrator` | Topological-layer execution scheduler. |
| `saga_registry` | `SagaRegistry` | Discovers and indexes `@saga`-decorated beans. |
| `saga_engine` | `SagaEngine` | Main saga engine (coordinates everything). |
| `tcc_registry` | `TccRegistry` | Discovers and indexes `@tcc`-decorated beans. |
| `tcc_engine` | `TccEngine` | Main TCC engine. |
| `saga_recovery_service` | `SagaRecoveryService` | Recovers stale in-flight sagas. |

### Conditional Wiring

The auto-configuration is activated by `@conditional_on_property`:

```python
@auto_configuration
@conditional_on_property("pyfly.transactional.enabled", having_value="true")
class TransactionalEngineAutoConfiguration:
    ...
```

When more advanced infrastructure is available:

* **Persistence**: If `pyfly.data` provides a database adapter, it replaces
  `InMemoryPersistenceAdapter`.
* **Events**: If `pyfly.eda` or `pyfly.observability` are available, a
  `CompositeEventsAdapter` is created that fans events to Logger + EDA +
  Metrics.

### Entry Point

Register the auto-configuration in `pyproject.toml`:

```toml
[project.entry-points."pyfly.auto_configuration"]
transactional = "pyfly.transactional.auto_configuration:TransactionalEngineAutoConfiguration"
```

### Enabling the Engine

Apply `@enable_transactional_engine` to your application's configuration
class:

```python
from pyfly.transactional import enable_transactional_engine
from pyfly.context.conditions import configuration

@enable_transactional_engine
@configuration
class AppConfig:
    pass
```

> **Key Point:** Inject `SagaEngine` or `TccEngine` by type. The DI
> container resolves all dependencies automatically.

---

## Complete Example: Order Fulfillment

This end-to-end example demonstrates a three-step saga for order
fulfillment: reserve inventory, process payment, and schedule shipping.

```python
from dataclasses import dataclass
from typing import Annotated, Any

from pyfly.container import component, service
from pyfly.transactional.saga.annotations import (
    saga,
    saga_step,
    Input,
    FromStep,
    Header,
    SetVariable,
    Variable,
)
from pyfly.transactional.saga.core.context import SagaContext
from pyfly.transactional.saga.core.result import SagaResult


# -- Domain types --

@dataclass(frozen=True)
class OrderRequest:
    customer_id: str
    items: list[str]
    total: float
    shipping_address: str

@dataclass(frozen=True)
class ReservationResult:
    reservation_id: str
    warehouse_id: str

@dataclass(frozen=True)
class PaymentResult:
    transaction_id: str
    charged_amount: float

@dataclass(frozen=True)
class ShippingResult:
    tracking_number: str


# -- Saga definition --

@saga(name="order-fulfillment", layer_concurrency=3)
@component
class OrderFulfillmentSaga:

    def __init__(
        self,
        inventory_service: InventoryService,
        payment_service: PaymentService,
        shipping_service: ShippingService,
    ) -> None:
        self._inventory = inventory_service
        self._payment = payment_service
        self._shipping = shipping_service

    # Step 1: Reserve inventory (no dependencies -- runs first)
    @saga_step(
        id="reserve-inventory",
        compensate="release_inventory",
        depends_on=[],
        retry=3,
        backoff_ms=200,
        timeout_ms=5000,
        jitter=True,
        jitter_factor=0.3,
    )
    async def reserve_inventory(
        self,
        request: Annotated[OrderRequest, Input],
        ctx: SagaContext,
    ) -> ReservationResult:
        return await self._inventory.reserve(
            items=request.items,
            correlation_id=ctx.correlation_id,
        )

    async def release_inventory(
        self,
        result: Annotated[ReservationResult, FromStep("reserve-inventory")],
    ) -> None:
        await self._inventory.release(result.reservation_id)

    # Step 2: Process payment (depends on inventory)
    @saga_step(
        id="process-payment",
        compensate="refund_payment",
        depends_on=["reserve-inventory"],
        retry=2,
        backoff_ms=500,
        timeout_ms=10000,
    )
    async def process_payment(
        self,
        request: Annotated[OrderRequest, Input],
        reservation: Annotated[ReservationResult, FromStep("reserve-inventory")],
        user_id: Annotated[str, Header("X-User-Id")],
    ) -> PaymentResult:
        return await self._payment.charge(
            customer_id=request.customer_id,
            amount=request.total,
            reservation_id=reservation.reservation_id,
        )

    async def refund_payment(
        self,
        payment: Annotated[PaymentResult, FromStep("process-payment")],
    ) -> None:
        await self._payment.refund(payment.transaction_id)

    # Step 3: Schedule shipping (depends on payment)
    @saga_step(
        id="schedule-shipping",
        compensate="cancel_shipping",
        depends_on=["process-payment"],
        retry=1,
        timeout_ms=8000,
    )
    async def schedule_shipping(
        self,
        request: Annotated[OrderRequest, Input],
        payment: Annotated[PaymentResult, FromStep("process-payment")],
    ) -> ShippingResult:
        return await self._shipping.schedule(
            address=request.shipping_address,
            transaction_id=payment.transaction_id,
        )

    async def cancel_shipping(
        self,
        result: Annotated[ShippingResult, FromStep("schedule-shipping")],
    ) -> None:
        await self._shipping.cancel(result.tracking_number)


# -- Execution --

@service
class OrderService:
    def __init__(self, saga_engine: SagaEngine) -> None:
        self._saga_engine = saga_engine

    async def place_order(self, request: OrderRequest, user_id: str) -> dict[str, Any]:
        result: SagaResult = await self._saga_engine.execute(
            saga_name="order-fulfillment",
            input_data=request,
            headers={"X-User-Id": user_id},
        )

        if result.success:
            shipping = result.result_of("schedule-shipping")
            return {
                "status": "confirmed",
                "tracking_number": shipping.tracking_number,
                "correlation_id": result.correlation_id,
            }
        else:
            failed = result.failed_steps()
            return {
                "status": "failed",
                "failed_steps": list(failed.keys()),
                "error": str(result.error),
                "correlation_id": result.correlation_id,
            }
```

### What Happens on Failure

If `process-payment` fails:

1. The engine detects the failure and switches to compensation mode.
2. `release_inventory` is called with the `ReservationResult` from step 1.
3. `SagaResult.success` is `False`, `failed_steps()` contains
   `"process-payment"`, and `compensated_steps()` contains
   `"reserve-inventory"`.

---

## Testing

### Testing Individual Steps

Test step logic in isolation by calling the method directly:

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_reserve_inventory_step() -> None:
    # Arrange
    saga = OrderFulfillmentSaga(
        inventory_service=AsyncMock(
            reserve=AsyncMock(return_value=ReservationResult("res-1", "wh-1"))
        ),
        payment_service=AsyncMock(),
        shipping_service=AsyncMock(),
    )
    ctx = SagaContext(correlation_id="test-123", saga_name="order-fulfillment")
    request = OrderRequest(
        customer_id="cust-1",
        items=["widget"],
        total=29.99,
        shipping_address="123 Main St",
    )

    # Act
    result = await saga.reserve_inventory(request, ctx)

    # Assert
    assert result.reservation_id == "res-1"
    saga._inventory.reserve.assert_awaited_once()
```

### Testing Compensation

```python
@pytest.mark.asyncio
async def test_release_inventory_compensation() -> None:
    saga = OrderFulfillmentSaga(
        inventory_service=AsyncMock(),
        payment_service=AsyncMock(),
        shipping_service=AsyncMock(),
    )
    reservation = ReservationResult("res-1", "wh-1")

    await saga.release_inventory(reservation)

    saga._inventory.release.assert_awaited_once_with("res-1")
```

### Testing Full Saga Execution

Use the real engine with mock services for integration testing:

```python
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.engine.argument_resolver import ArgumentResolver
from pyfly.transactional.saga.engine.step_invoker import StepInvoker
from pyfly.transactional.saga.engine.compensator import SagaCompensator
from pyfly.transactional.saga.engine.execution_orchestrator import SagaExecutionOrchestrator
from pyfly.transactional.saga.registry.saga_registry import SagaRegistry
from pyfly.transactional.shared.persistence.memory import InMemoryPersistenceAdapter
from pyfly.transactional.shared.observability.events import LoggerEventsAdapter

@pytest.fixture
def saga_engine() -> SagaEngine:
    resolver = ArgumentResolver()
    invoker = StepInvoker(argument_resolver=resolver)
    events = LoggerEventsAdapter()
    compensator = SagaCompensator(step_invoker=invoker, events_port=events)
    orchestrator = SagaExecutionOrchestrator(step_invoker=invoker, events_port=events)
    persistence = InMemoryPersistenceAdapter()
    registry = SagaRegistry()

    return SagaEngine(
        registry=registry,
        step_invoker=invoker,
        execution_orchestrator=orchestrator,
        compensator=compensator,
        persistence_port=persistence,
        events_port=events,
    )

@pytest.mark.asyncio
async def test_saga_happy_path(saga_engine: SagaEngine) -> None:
    result = await saga_engine.execute(
        saga_name="order-fulfillment",
        input_data=OrderRequest("cust-1", ["A"], 9.99, "123 Main St"),
        headers={"X-User-Id": "user-42"},
    )
    assert result.success
    assert result.result_of("schedule-shipping") is not None
```

### Testing TCC Transactions

```python
@pytest.mark.asyncio
async def test_tcc_payment_happy_path() -> None:
    tcc = OrderPaymentTcc(
        payment_service=AsyncMock(
            reserve=AsyncMock(return_value="rsv-1"),
            commit=AsyncMock(),
        ),
    )
    participant = tcc.PaymentParticipant()

    # Test try phase
    ctx = TccContext(tcc_name="order-payment")
    result = await participant.try_reserve(payment_request, ctx)
    assert result == "rsv-1"

    # Test confirm phase
    await participant.confirm(result, ctx)
    tcc._payment_service.commit.assert_awaited_once_with("rsv-1")
```

### Testing with SagaBuilder

The programmatic builder is especially convenient in tests:

```python
@pytest.mark.asyncio
async def test_dynamic_saga() -> None:
    saga_def = (
        SagaBuilder("test-saga")
        .step("step-a").handler(my_step_a_fn).add()
        .step("step-b").handler(my_step_b_fn).depends_on("step-a").add()
        .build()
    )

    assert len(saga_def.steps) == 2
    assert saga_def.steps["step-b"].depends_on == ["step-a"]
```

> **Key Point:** The in-memory persistence adapter and logger events adapter
> have no external dependencies, making them ideal for test fixtures. You
> can construct the full engine in a test without any mocking of
> infrastructure.

---

## Java Comparison

The `pyfly.transactional` module is a 1:1 feature port of the
`fireflyframework-transactional-engine` Java/Spring Boot library. The table
below shows how key concepts translate.

| Concept | Java (Spring Boot) | Python (PyFly) |
|---------|--------------------|----------------|
| **Async model** | Project Reactor (`Mono<T>`, `Flux<T>`) | Native `asyncio` (`async/await`, `gather`, `TaskGroup`) |
| **Saga definition** | `@Saga(name="...")` annotation on class | `@saga(name="...")` decorator on class |
| **Step definition** | `@SagaStep(id="...", compensate="...")` on method | `@saga_step(id="...", compensate="...")` on method |
| **Input injection** | `@Input OrderRequest req` | `Annotated[OrderRequest, Input]` |
| **Step result injection** | `@FromStep("step-id") T result` | `Annotated[T, FromStep("step-id")]` |
| **Header injection** | `@Header("X-User-Id") String userId` | `Annotated[str, Header("X-User-Id")]` |
| **Context injection** | `SagaContext ctx` parameter | `ctx: SagaContext` parameter |
| **TCC definition** | `@Tcc(name="...")` on class | `@tcc(name="...")` on class |
| **TCC participant** | `@TccParticipant(id="...")` on inner class | `@tcc_participant(id="...")` on nested class |
| **TCC phases** | `@TryMethod`, `@ConfirmMethod`, `@CancelMethod` | `@try_method(...)`, `@confirm_method(...)`, `@cancel_method(...)` |
| **Try result injection** | `@FromTry ReservationId id` | `Annotated[ReservationId, FromTry()]` |
| **Configuration** | `@ConfigurationProperties(prefix="...")` | `@config_properties(prefix="...")` + YAML |
| **DI** | Spring `@Component` + `@Bean` | PyFly `@component` + `@bean` |
| **Auto-configuration** | `@AutoConfiguration` + `@ConditionalOnProperty` | `@auto_configuration` + `@conditional_on_property` |
| **Compensation policies** | `CompensationPolicy` enum (5 values) | `CompensationPolicy` enum (same 5 values) |
| **Backpressure** | Reactor Schedulers + custom strategies | `asyncio.Semaphore` + Adaptive/Batched/CircuitBreaker |
| **Circuit breaker** | Resilience4j | Native implementation in `SagaCompensator` and `CircuitBreakerBackpressureStrategy` |
| **Saga builder** | `SagaBuilder` fluent API | `SagaBuilder` fluent API (identical pattern) |
| **Saga composition** | `SagaCompositionBuilder` | `SagaCompositionBuilder` (identical pattern) |
| **Persistence** | Spring Data JPA / R2DBC | `TransactionalPersistencePort` protocol |
| **Recovery** | `SagaRecoveryService` scheduled task | `SagaRecoveryService` async method |
| **DAG validation** | Kahn's algorithm at startup | Kahn's algorithm at startup |
| **Concurrency control** | `Semaphore` in Reactor | `asyncio.Semaphore` |
| **Timeout** | `Mono.timeout()` | `asyncio.wait_for()` |
| **Entry point registration** | `META-INF/spring.factories` | `pyproject.toml` entry points |

The core difference is the async model: Java uses Project Reactor's reactive
streams (non-blocking but callback-heavy), while Python uses `asyncio`'s
native `async/await` (sequential-looking code that runs concurrently). The
API surface, configuration model, and extensibility points are otherwise
identical.

---

## Workflow Pattern (new in v0.3.0)

The **Workflow** pattern joins Saga and TCC as the third orchestration
primitive. Where Saga is sequential-with-compensation and TCC is
two-phase-commit-with-reservations, **Workflow is signal-driven and
long-running** — perfect for human-in-the-loop processes (loan
approvals, manual reviews, multi-day waits) and DAG fan-out / fan-in.

It mirrors the Java engine's `org.fireflyframework.orchestration.workflow`
package one-to-one.

### Decorators

```python
from pyfly.transactional.workflow import (
    workflow, workflow_step,
    wait_for_signal, wait_for_timer, wait_for_all, wait_for_any,
    child_workflow, compensation_step, workflow_query,
    on_workflow_complete, on_workflow_error, on_step_complete,
    scheduled_workflow,
)
```

### Definition

```python
@workflow(id="loanApproval", trigger_event_type="LoanRequested",
          trigger_mode=TriggerMode.SYNC, timeout_ms=86_400_000)
class LoanApproval:
    @workflow_step(id="enrich")
    async def enrich(self, payload): ...

    @workflow_step(id="autoCheck", depends_on=["enrich"])
    async def auto_check(self): ...

    @workflow_step(id="manualReview", depends_on=["autoCheck"], compensatable=True,
                   compensation_method="releaseReview")
    @wait_for_signal("approved", timeout_ms=86_400_000)
    async def manual_review(self): ...

    @compensation_step(for_step="manualReview")
    async def releaseReview(self): ...

    @workflow_step(id="cooldown", depends_on=["manualReview"])
    @wait_for_timer(delay_ms=60_000)
    async def cooldown(self): ...

    @workflow_step(id="payout", depends_on=["cooldown"])
    @child_workflow(workflow_id="payoutFlow", wait_for_completion=True)
    async def payout(self): ...

    @workflow_query()
    async def status(self, ctx): return ctx.status

    @on_workflow_complete
    async def done(self, ctx): ...

    @on_workflow_error
    async def errored(self, ctx, err): ...
```

### Driving the engine

```python
result = await workflow_engine.start("loanApproval", input={"amount": 5000})
# Suspended on `wait_for_signal`; somewhere later:
await workflow_engine.deliver_signal(result.correlation_id, "approved",
                                     payload={"by": "manager-A"})
# Read-side query into the running workflow:
status = await workflow_engine.query(result.correlation_id, "status")
```

### Programmatic builder

```python
from pyfly.transactional.workflow.builder import WorkflowBuilder

definition = (
    WorkflowBuilder("paymentFlow")
    .step("validate", validate_handler)
    .step("authorize", authorize_handler, depends_on=["validate"])
    .wait_signal("approved", "human-approved", depends_on=["authorize"])
    .step("settle", settle_handler, depends_on=["approved"])
    .build()
)
workflow_registry._definitions[definition.id] = definition  # or expose register_definition
```

### Persistence + REST

The workflow engine persists `ExecutionContext` after every layer through
the configured `ExecutionPersistenceProvider` (in-memory, Redis,
SQLAlchemy or cache). The REST controllers expose:

```
POST   /api/orchestration/workflow/start                 # body: {workflow_id, input}
POST   /api/orchestration/workflow/signal                # body: {correlation_id, signal, payload}

GET    /api/orchestration/executions                     # in-flight only by default; ?status=<S> filters exactly
GET    /api/orchestration/executions/{correlation_id}

GET    /api/orchestration/dlq                             # ?execution_name=… &correlation_id=… filter
GET    /api/orchestration/dlq/count                       # {"count": <int>}
GET    /api/orchestration/dlq/{entry_id}
POST   /api/orchestration/dlq/{entry_id}/retry
DELETE /api/orchestration/dlq/{entry_id}
```

### Execution listing semantics

`GET /api/orchestration/executions` with **no** `status` query parameter
returns only **in-flight** (non-terminal) executions — it no longer surfaces
the whole store including terminal history (Java parity). Supplying an explicit
`?status=<STATUS>` filters the store to exactly that status.

### Dead-letter queue

`GET /api/orchestration/dlq/count` returns `{"count": <int>}` — the cardinality
of dead-lettered runs, backed by `DeadLetterService.count()` — for ops
dashboards. The `dlq` collection also supports listing (optionally filtered by
`execution_name` / `correlation_id`), fetching a single entry, marking an entry
retried, and deleting an entry.
