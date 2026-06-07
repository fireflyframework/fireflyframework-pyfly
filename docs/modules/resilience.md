# Resilience Guide

Protect your services with rate limiting, bulkhead isolation, timeouts, and
fallback strategies using the PyFly resilience module.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Retry](#retry)
   - [@retry Decorator](#retry-decorator)
   - [Backoff, Cap, and Jitter](#backoff-cap-and-jitter)
   - [Filtering Exception Types](#filtering-which-exceptions-retry)
3. [Circuit Breaker](#circuit-breaker)
   - [CircuitBreaker Class](#circuitbreaker-class)
   - [Count-Based vs Rate-Based Tripping](#count-based-vs-rate-based-tripping)
   - [Half-Open Recovery](#half-open-recovery)
   - [@circuit_breaker Decorator](#circuit_breaker-decorator)
4. [Rate Limiter](#rate-limiter)
   - [RateLimiter Class](#ratelimiter-class)
   - [Token Bucket Algorithm](#token-bucket-algorithm)
   - [@rate_limiter Decorator](#rate_limiter-decorator)
5. [Bulkhead](#bulkhead)
   - [Bulkhead Class](#bulkhead-class)
   - [Permit-Counter Concurrency Limiting](#permit-counter-concurrency-limiting)
   - [@bulkhead Decorator](#bulkhead-decorator)
6. [Time Limiter](#time-limiter)
   - [@time_limiter Decorator](#time_limiter-decorator)
   - [How It Works](#how-it-works)
7. [Fallback](#fallback)
   - [@fallback Decorator](#fallback-decorator)
   - [Fallback with a Method](#fallback-with-a-method)
   - [Fallback with a Static Value](#fallback-with-a-static-value)
   - [Filtering Exception Types](#filtering-exception-types)
8. [Exception Types](#exception-types)
9. [Combining Patterns](#combining-patterns)
   - [Stacking Decorators](#stacking-decorators)
   - [Recommended Order](#recommended-order)
10. [Configuration](#configuration)
11. [Complete Example](#complete-example)

---

## Introduction

In distributed systems, failures are inevitable. A downstream service can slow
down, a database can run out of connections, or a burst of traffic can overwhelm
an endpoint. Without protection, these failures cascade -- one slow dependency
brings down the entire system.

Resilience patterns address this by setting boundaries on how your code
interacts with unreliable resources:

| Pattern | Purpose |
|---|---|
| **Retry** | Re-invokes a failing call with backoff to ride out transient errors |
| **Circuit Breaker** | Stops calling a failing dependency to let it recover |
| **Rate Limiter** | Caps the number of calls in a time window to prevent overload |
| **Bulkhead** | Limits concurrent executions to isolate failures |
| **Time Limiter** | Enforces a maximum execution time to avoid hanging calls |
| **Fallback** | Provides a degraded response when the primary path fails |

PyFly implements each pattern as both a standalone class (for programmatic use)
and a decorator (for declarative use on async functions). All resilience types
are available from a single import:

```python
from pyfly.resilience import (
    retry,
    CircuitBreaker,
    CircuitState,
    circuit_breaker,
    RateLimiter,
    rate_limiter,
    Bulkhead,
    bulkhead,
    time_limiter,
    fallback,
)
```

---

## Retry

The retry pattern re-invokes a callable that raises a transient error, sleeping
between attempts so the dependency has time to recover. It is the PyFly
equivalent of Spring Retry / Resilience4j `@Retry`.

### @retry Decorator

`retry()` returns a decorator that works on **both sync and async** callables
(it detects coroutine functions and adapts the wait between attempts):

```python
from pyfly.resilience import retry

@retry(max_attempts=3, delay=0.2, backoff=2.0)
async def fetch_user(user_id: str) -> dict:
    return await http_client.get(f"/users/{user_id}")
```

**Parameters** (`max_attempts` is positional; the rest are keyword-only):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_attempts` | `int` | `3` | Total attempts **including the first** (must be `>= 1`, else `ValueError`). |
| `delay` | `float` | `0.0` | Base delay in seconds before the first retry. |
| `backoff` | `float` | `1.0` | Multiplier applied to the delay each subsequent attempt. |
| `max_delay` | `float \| None` | `None` | Optional cap on the per-attempt wait. |
| `jitter` | `float` | `0.0` | Randomization fraction in `[0, 1]` applied as `±jitter * wait`. |
| `exceptions` | `tuple[type[BaseException], ...]` | `(Exception,)` | Exception types that trigger a retry; others propagate immediately. |

After all attempts are exhausted, the **last** exception is re-raised. With the
default `delay=0.0` there is no sleep between attempts.

### Backoff, Cap, and Jitter

The wait before retry number `attempt` (0-indexed) is:

```
wait = delay * (backoff ** attempt)        # exponential growth
wait += random.uniform(-jitter, jitter) * wait   # if jitter > 0
wait = min(wait, max_delay)                # if max_delay is set
```

So exponential backoff with a ceiling and anti-thundering-herd jitter looks
like this:

```python
@retry(max_attempts=5, delay=0.5, backoff=2.0, max_delay=10.0, jitter=0.2)
async def call_flaky_service() -> str:
    # waits ~0.5s, ~1.0s, ~2.0s, ~4.0s (each ±20%, capped at 10s)
    return await client.invoke()
```

### Filtering Which Exceptions Retry

Pass `exceptions` to retry only on specific error types. Anything not listed
propagates on the first occurrence without retrying:

```python
from pyfly.kernel.exceptions import OperationTimeoutException

@retry(max_attempts=4, delay=0.1, exceptions=(ConnectionError, OperationTimeoutException))
async def load_config() -> dict:
    return await remote_config.get()
```

Here a `ValueError` would surface immediately, while `ConnectionError` or
`OperationTimeoutException` are retried up to four total attempts.

---

## Circuit Breaker

A circuit breaker stops calling a dependency that is failing, giving it room to
recover instead of hammering it with doomed requests. It is a thread-safe
state machine with three states (`CircuitState.CLOSED`, `OPEN`, `HALF_OPEN`) and
is the PyFly equivalent of Resilience4j's circuit breaker.

### CircuitBreaker Class

```python
from pyfly.resilience import CircuitBreaker

breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
```

**Constructor parameters** (`failure_rate_threshold`, `window_size`, and
`half_open_max_calls` are keyword-only):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `failure_threshold` | `int` | `5` | Consecutive failures that trip the circuit (used when `failure_rate_threshold` is `None`). |
| `recovery_timeout` | `float` | `30.0` | Seconds the circuit stays `OPEN` before moving to `HALF_OPEN`. |
| `expected` | `tuple[type[BaseException], ...]` | `(Exception,)` | Exception types that count as failures; others pass through without affecting the circuit. |
| `failure_rate_threshold` | `float \| None` | `None` | When set, trip on failure *rate* over the window (COUNT_BASED window) instead of consecutive count. |
| `window_size` | `int` | `10` | Size of the sliding outcome window used for rate-based tripping. |
| `half_open_max_calls` | `int` | `1` | Trial calls admitted in `HALF_OPEN`; this many successes close the circuit (coerced to `>= 1`). |

The current state is read via the `state` property, which also performs the
lazy `OPEN -> HALF_OPEN` transition once `recovery_timeout` has elapsed:

```python
from pyfly.resilience import CircuitState

if breaker.state is CircuitState.OPEN:
    ...
```

### Count-Based vs Rate-Based Tripping

By default the breaker trips after `failure_threshold` **consecutive** failures;
a single success resets the counter:

```python
# Trip after 5 consecutive failures (default behavior)
breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
```

Set `failure_rate_threshold` to switch to a Resilience4j-style **COUNT_BASED**
window: the breaker trips once the failure rate over the last `window_size`
calls reaches the threshold. Two conditions must both hold before a rate-based
trip occurs:

1. The window must be **full** (`window_size` outcomes recorded) — a partial
   window is never judged.
2. The threshold is checked when a **failure** is recorded, so the call that
   completes the window and pushes the rate over the line must itself be a
   failure.

```python
# Trip when 50% of the last 10 calls failed
breaker = CircuitBreaker(
    recovery_timeout=15.0,
    failure_rate_threshold=0.5,
    window_size=10,
    expected=(ConnectionError,),
)
```

### Half-Open Recovery

After `recovery_timeout` seconds in `OPEN`, the next state read moves the
circuit to `HALF_OPEN`, which admits up to `half_open_max_calls` trial calls.
Excess probes are rejected with `CircuitBreakerException`. If those trials all
succeed the circuit closes; any failure during `HALF_OPEN` re-opens it
immediately:

```python
# Require 2 successful trial calls to close the circuit again
breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=10.0,
    half_open_max_calls=2,
)
```

### @circuit_breaker Decorator

`circuit_breaker(breaker)` guards a callable (sync or async) with an existing
`CircuitBreaker` instance. It rejects calls while the circuit is `OPEN` (or the
half-open probe budget is exhausted) by raising `CircuitBreakerException`, and
records success/failure otherwise. Only exceptions in `breaker.expected` count
as failures:

```python
from pyfly.resilience import CircuitBreaker, circuit_breaker
from pyfly.kernel.exceptions import CircuitBreakerException

inventory_breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=20.0,
    expected=(ConnectionError,),
)

@circuit_breaker(inventory_breaker)
async def check_stock(sku: str) -> int:
    return await inventory_api.count(sku)

try:
    qty = await check_stock("ABC-123")
except CircuitBreakerException:
    qty = 0  # circuit is open — serve a degraded value
```

Share a single `CircuitBreaker` instance across multiple functions to trip them
together, or pair `@circuit_breaker` with `@retry` and `@fallback` (see
[Combining Patterns](#combining-patterns)).

---

## Rate Limiter

The rate limiter prevents a function from being called more often than allowed.
PyFly uses a **token bucket** algorithm, which provides smooth rate limiting
with configurable burst capacity.

### RateLimiter Class

```python
from pyfly.resilience import RateLimiter

limiter = RateLimiter(max_tokens=10, refill_rate=10.0)
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_tokens` | `int` | `10` | Maximum bucket capacity (burst size). The bucket starts full. |
| `refill_rate` | `float` | `10.0` | Number of tokens added per second. |

**Methods and properties:**

| Member | Description |
|---|---|
| `await acquire()` | Consume one token. Raises `RateLimitException` if none available. |
| `available_tokens` | Property returning the approximate number of tokens currently available. |

**Programmatic usage:**

```python
from pyfly.kernel.exceptions import RateLimitException

limiter = RateLimiter(max_tokens=5, refill_rate=2.0)

try:
    await limiter.acquire()
    # Proceed with the operation
    result = await call_external_api()
except RateLimitException:
    # Handle rate limit exceeded
    return {"error": "Too many requests"}
```

### Token Bucket Algorithm

The token bucket works as follows:

1. The bucket starts with `max_tokens` tokens.
2. Each call to `acquire()` consumes one token.
3. Tokens are refilled at `refill_rate` tokens per second, up to `max_tokens`.
4. Refill is calculated lazily on each `acquire()` call based on elapsed time
   since the last refill, using `time.monotonic()`.
5. If fewer than 1.0 tokens are available, `RateLimitException` is raised
   immediately (no waiting or queueing).

The lazy refill means there is no background timer. Tokens accumulate based on
the wall-clock difference between calls. This is both efficient and accurate.

**Example -- 100 requests/minute with burst of 20:**

```python
limiter = RateLimiter(
    max_tokens=20,        # Allow bursts of up to 20 requests
    refill_rate=100 / 60  # ~1.67 tokens/second = 100/minute sustained
)
```

Thread safety is ensured via an `asyncio.Lock` that serializes access to the
token count during `acquire()`.

### @rate_limiter Decorator

The `rate_limiter()` function returns a decorator that wraps an async function
with automatic token acquisition:

```python
from pyfly.resilience import RateLimiter, rate_limiter

api_limiter = RateLimiter(max_tokens=50, refill_rate=10.0)

@rate_limiter(api_limiter)
async def fetch_user(user_id: str) -> dict:
    return await http_client.get(f"/users/{user_id}")
```

The decorator calls `await limiter.acquire()` before every invocation of the
wrapped function. If the token bucket is empty, `RateLimitException` propagates
to the caller. The original function's signature and docstring are preserved
via `functools.wraps`.

Multiple functions can share the same `RateLimiter` instance to enforce a
global rate across different endpoints:

```python
shared_limiter = RateLimiter(max_tokens=100, refill_rate=50.0)

@rate_limiter(shared_limiter)
async def endpoint_a(): ...

@rate_limiter(shared_limiter)
async def endpoint_b(): ...
```

---

## Bulkhead

The bulkhead pattern isolates concurrent access to a resource. It prevents a
single slow dependency from consuming all available connections or threads,
leaving room for other operations to proceed. The name comes from ship
bulkheads -- watertight compartments that prevent a single breach from sinking
the entire vessel.

### Bulkhead Class

```python
from pyfly.resilience import Bulkhead

bh = Bulkhead(max_concurrent=10)
```

**Constructor parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `max_concurrent` | `int` | `10` | Maximum number of concurrent calls allowed. |

**Methods and properties:**

| Member | Description |
|---|---|
| `await acquire()` | Try to acquire a slot. Raises `BulkheadException` if at capacity. |
| `release()` | Release a slot back to the pool. |
| `available_slots` | Property: number of free slots. |
| `max_concurrent` | Property: maximum configured concurrency. |

**Programmatic usage:**

```python
from pyfly.kernel.exceptions import BulkheadException

bh = Bulkhead(max_concurrent=5)

try:
    await bh.acquire()
    try:
        result = await slow_database_query()
    finally:
        bh.release()
except BulkheadException:
    return {"error": "Service at capacity, try again later"}
```

### Permit-Counter Concurrency Limiting

Internally, `Bulkhead` uses a single lock-guarded permit counter (`active`
count protected by a `threading.Lock`) as the *sole* source of truth. The same
primitive is shared by both sync- and async-decorated calls, so a single
`Bulkhead` instance can decorate a mix of sync and async functions without the
two paths ever desynchronising. The critical design choice is that `acquire()`
is **non-blocking**: it checks the active count *before* incrementing it. If
all slots are taken, it raises `BulkheadException` immediately rather than
queueing the caller -- matching Resilience4j's zero-wait semaphore bulkhead.

This fail-fast behavior is intentional. In a microservice context, it is better
to reject a request quickly and let the caller retry or use a fallback than to
queue requests indefinitely, which can cause cascading backpressure.

### @bulkhead Decorator

The `bulkhead()` function returns a decorator that automatically acquires a
slot before execution and releases it afterward (even if an exception is
raised):

```python
from pyfly.resilience import Bulkhead, bulkhead

db_bulkhead = Bulkhead(max_concurrent=20)

@bulkhead(db_bulkhead)
async def query_database(sql: str) -> list[dict]:
    return await db.execute(sql)
```

The decorator wraps the function in a `try/finally` block so that `release()`
is always called, even if the wrapped function raises an exception. This
prevents slot leaks.

---

## Time Limiter

The time limiter enforces a maximum execution duration on an async function.
If the function does not complete within the allotted time, it is cancelled and
an `OperationTimeoutException` is raised.

### @time_limiter Decorator

```python
from datetime import timedelta
from pyfly.resilience import time_limiter

@time_limiter(timeout=timedelta(seconds=5))
async def fetch_recommendations(user_id: str) -> list:
    return await recommendation_engine.get(user_id)
```

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `timeout` | `timedelta` | Maximum allowed execution time. |

If `fetch_recommendations` takes longer than 5 seconds, the coroutine is
cancelled and `OperationTimeoutException` is raised with a descriptive message:

```
OperationTimeoutException: fetch_recommendations exceeded timeout of 5.0s
```

### How It Works

Under the hood, `time_limiter` uses `asyncio.wait_for()`:

```python
# Simplified internal logic
try:
    return await asyncio.wait_for(func(*args, **kwargs), timeout=timeout_seconds)
except asyncio.TimeoutError as exc:
    raise OperationTimeoutException(
        f"{func.__name__} exceeded timeout of {timeout_seconds}s"
    ) from exc
```

`asyncio.wait_for()` cancels the underlying task when the timeout expires,
which means any `await` inside the function will raise `asyncio.CancelledError`.
Resources acquired inside the function should be protected with `try/finally`
blocks:

```python
@time_limiter(timeout=timedelta(seconds=10))
async def process_with_cleanup():
    conn = await pool.acquire()
    try:
        return await conn.execute(query)
    finally:
        await pool.release(conn)  # Always release, even on timeout
```

---

## Fallback

The fallback pattern provides a degraded but functional response when the
primary code path fails. It catches exceptions and returns an alternative result
instead of propagating the error.

### @fallback Decorator

```python
from pyfly.resilience import fallback
```

The `fallback()` function accepts keyword-only parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `fallback_method` | `Callable` | `None` | A function to call on failure. Receives the same args plus `exc` kwarg. |
| `fallback_value` | `Any` | `None` | A static value to return on failure. |
| `on` | `tuple[type[Exception], ...]` | `(Exception,)` | Exception types to catch. |

You must provide exactly one of `fallback_method` or `fallback_value`. Providing
neither raises `ValueError`.

### Fallback with a Method

When `fallback_method` is specified, it is called with the same positional and
keyword arguments as the original function, plus an additional `exc` keyword
argument containing the caught exception:

```python
async def cached_price(product_id: str, exc: Exception = None) -> float:
    """Return cached price when the live pricing service is down."""
    return await cache.get(f"price:{product_id}", default=0.0)

@fallback(fallback_method=cached_price)
async def get_price(product_id: str) -> float:
    return await pricing_service.get_price(product_id)
```

If `get_price` raises any exception, `cached_price` is called with the same
`product_id` and the exception as `exc`. The fallback method can be either sync
or async -- if it returns an awaitable, PyFly will automatically await it.

### Fallback with a Static Value

For simple cases, you can return a fixed default value:

```python
@fallback(fallback_value=[])
async def get_recommendations(user_id: str) -> list:
    return await recommendation_engine.get(user_id)
```

If `get_recommendations` fails, the caller receives an empty list instead of
an exception.

### Filtering Exception Types

By default, `fallback` catches all exceptions (`Exception`). Use the `on`
parameter to restrict which exceptions trigger the fallback:

```python
from pyfly.kernel.exceptions import OperationTimeoutException, CircuitBreakerException

@fallback(
    fallback_value={"status": "degraded"},
    on=(OperationTimeoutException, CircuitBreakerException),
)
async def get_status():
    return await health_service.check()
```

Only `OperationTimeoutException` and `CircuitBreakerException` will trigger
the fallback. Other exceptions propagate normally.

---

## Exception Types

All resilience-related exceptions inherit from `InfrastructureException` in
`pyfly.kernel.exceptions`:

```python
from pyfly.kernel.exceptions import (
    RateLimitException,
    BulkheadException,
    OperationTimeoutException,
    CircuitBreakerException,
)
```

| Exception | Raised by | Message example |
|---|---|---|
| `RateLimitException` | `RateLimiter.acquire()` | `"Rate limit exceeded"` |
| `BulkheadException` | `Bulkhead.acquire()` | `"Bulkhead at capacity (10 concurrent calls)"` |
| `OperationTimeoutException` | `time_limiter` | `"fetch_data exceeded timeout of 5.0s"` |
| `CircuitBreakerException` | `CircuitBreaker.before_call()` / `@circuit_breaker` | `"Circuit breaker is open"` |

These are all subclasses of `InfrastructureException`, which itself extends
`PyFlyException`. You can catch them individually or catch the parent class for
broad infrastructure error handling:

```python
from pyfly.kernel.exceptions import InfrastructureException

try:
    result = await protected_operation()
except InfrastructureException as exc:
    logger.warning("Infrastructure issue: %s", exc)
    return fallback_result
```

For the circuit breaker and retry patterns, see the [Retry](#retry) and
[Circuit Breaker](#circuit-breaker) sections above. The [HTTP Client
module](client.md) layers its own retry/circuit-breaking on top of outbound
requests.

---

## Combining Patterns

Real-world services typically need multiple resilience patterns working
together. PyFly decorators compose naturally by stacking.

### Stacking Decorators

```python
from datetime import timedelta
from pyfly.resilience import (
    RateLimiter,
    Bulkhead,
    rate_limiter,
    bulkhead,
    time_limiter,
    fallback,
)

api_limiter = RateLimiter(max_tokens=100, refill_rate=20.0)
api_bulkhead = Bulkhead(max_concurrent=10)

@fallback(fallback_value={"status": "unknown"})
@rate_limiter(api_limiter)
@bulkhead(api_bulkhead)
@time_limiter(timeout=timedelta(seconds=5))
async def check_external_service() -> dict:
    return await external_api.health()
```

### Recommended Order

Decorator order matters. Decorators are applied bottom-up (the lowest decorator
wraps the function first), but execute top-down (the topmost decorator runs
first when the function is called). The recommended stacking order from
outermost (top) to innermost (bottom):

```
@fallback          -- 1. Catch any exception and provide a fallback
@rate_limiter      -- 2. Reject excess traffic before it consumes resources
@bulkhead          -- 3. Limit concurrent access to the resource
@time_limiter      -- 4. Cancel if the operation takes too long
async def func():  -- 5. The actual operation
```

**Why this order:**

1. **Fallback (outermost)**: Catches exceptions from all inner layers,
   including `RateLimitException` and `BulkheadException`. This ensures the
   caller always gets a response.

2. **Rate limiter**: Rejects excess requests before they even attempt to
   acquire a bulkhead slot. This prevents a flood of requests from exhausting
   the bulkhead.

3. **Bulkhead**: Limits how many of the rate-limited requests can actually
   execute concurrently.

4. **Time limiter (innermost)**: Applies only to the actual execution. If
   the function is slow, it gets cancelled, and the bulkhead slot is released
   via the `finally` block.

---

## Configuration

Resilience settings can be configured in `pyfly.yaml`:

```yaml
pyfly:
  resilience:
    rate-limiter:
      default:
        max-tokens: 100
        refill-rate: 50.0
    bulkhead:
      default:
        max-concurrent: 20
    time-limiter:
      default:
        timeout: 5s
```

| Key | Description | Default |
|---|---|---|
| `pyfly.resilience.rate-limiter.default.max-tokens` | Default bucket capacity | `10` |
| `pyfly.resilience.rate-limiter.default.refill-rate` | Default tokens/second | `10.0` |
| `pyfly.resilience.bulkhead.default.max-concurrent` | Default max concurrency | `10` |
| `pyfly.resilience.time-limiter.default.timeout` | Default timeout duration | `30s` |

Named configurations can be created for different services:

```yaml
pyfly:
  resilience:
    rate-limiter:
      payment-api:
        max-tokens: 10
        refill-rate: 2.0
      search-api:
        max-tokens: 200
        refill-rate: 100.0
```

---

## Complete Example

Here is a complete example that protects an API endpoint with all four
resilience patterns working together. The scenario: a product catalog service
that fetches prices from an external pricing API.

```python
from datetime import timedelta

from pyfly.container import service
from pyfly.kernel.exceptions import (
    BulkheadException,
    OperationTimeoutException,
    RateLimitException,
)
from pyfly.resilience import (
    Bulkhead,
    RateLimiter,
    bulkhead,
    fallback,
    rate_limiter,
    time_limiter,
)


# --- Resilience infrastructure ---

# Allow 50 requests/second with burst up to 100
pricing_limiter = RateLimiter(max_tokens=100, refill_rate=50.0)

# At most 10 concurrent calls to the pricing API
pricing_bulkhead = Bulkhead(max_concurrent=10)


# --- Fallback handler ---

async def pricing_fallback(product_id: str, exc: Exception = None) -> dict:
    """Return cached/default pricing when the live API is unavailable."""
    # In a real app, this would check a local cache
    return {
        "product_id": product_id,
        "price": 0.0,
        "currency": "USD",
        "source": "fallback",
        "reason": str(exc),
    }


# --- Protected service ---

@service
class PricingService:
    def __init__(self, http_client):
        self.http_client = http_client

    @fallback(fallback_method=pricing_fallback)
    @rate_limiter(pricing_limiter)
    @bulkhead(pricing_bulkhead)
    @time_limiter(timeout=timedelta(seconds=3))
    async def get_price(self, product_id: str) -> dict:
        """Fetch live pricing from external API.

        Protected by four resilience layers:
        1. Fallback: returns cached data on any failure
        2. Rate limiter: caps at 50 req/s sustained, 100 burst
        3. Bulkhead: max 10 concurrent pricing calls
        4. Time limiter: 3-second timeout per call
        """
        response = await self.http_client.get(f"/prices/{product_id}")
        return response.json()
```

### Monitoring Resilience State

You can inspect the state of resilience components at runtime for
observability and health checks:

```python
# Check rate limiter capacity
tokens = pricing_limiter.available_tokens
print(f"Available tokens: {tokens:.1f}")

# Check bulkhead utilization
slots = pricing_bulkhead.available_slots
max_slots = pricing_bulkhead.max_concurrent
utilization = 1.0 - (slots / max_slots)
print(f"Bulkhead utilization: {utilization:.0%}")
print(f"Available slots: {slots}/{max_slots}")
```

### Independent Usage Without Decorators

Every resilience primitive can be used programmatically without decorators,
giving you fine-grained control in complex workflows:

```python
limiter = RateLimiter(max_tokens=10, refill_rate=5.0)
bh = Bulkhead(max_concurrent=3)

async def complex_workflow(items: list[str]) -> list[dict]:
    results = []
    for item in items:
        # Rate limit
        await limiter.acquire()

        # Bulkhead
        await bh.acquire()
        try:
            result = await process_item(item)
            results.append(result)
        finally:
            bh.release()

    return results
```

This flexibility means the decorator syntax is a convenience layer over
fully composable building blocks.
