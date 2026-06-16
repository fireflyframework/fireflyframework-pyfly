<span class="eyebrow">Chapter 11</span>

# Splitting the Monolith: HTTP Clients & the BFF {.chtitle}

::: figure art/openers/ch11.svg | &nbsp;

When Lumen was a single service, every capability lived in the same
process. The wallet, the balance check, and the payment processing all
ran together — straightforward to test, simple to deploy, and perfectly
adequate until the team needed to ship features at different cadences.
Then came the hard conversation about splitting.

The promise of a microservice split is real: teams own their services
independently, scale them separately, and deploy without coordinating a
shared release window. But every split introduces a problem the monolith
never had — the network. What was a local function call becomes an HTTP
request that can time out, fail halfway, or land on an overwhelmed
service. That network boundary is not a deployment detail; it is a
first-class engineering concern.

This chapter introduces `PaymentsService`, a second service that
Lumen's Wallet service calls to settle transfers. Instead of hand-rolling
`httpx` sessions and threading circuit-breaker logic through every
handler, you will define the Payments client as an ordinary Python class
— a typed, declarative interface that PyFly fills in at startup. By the
end of the chapter you will also see how a **BFF (Backend for Frontend)**
tier sits in front of both services and composes their capabilities into
a single, user-journey-focused API.

!!! note "New jargon, in plain language"
    A few terms recur throughout this chapter. **Service-to-service call**
    means one of your services making an HTTP request to another of your
    services (Wallet calling Payments), as opposed to a request coming
    from a browser. A **declarative client** is a client you *describe*
    rather than *implement*: you write the method signatures and let the
    framework fill in the HTTP plumbing. A **circuit breaker** is a safety
    switch that stops calling a remote service after it has failed too
    many times in a row. A **BFF** (Backend for Frontend) is a thin
    service whose only job is to call other services and reshape their
    answers for one particular app. We will build each of these one piece
    at a time.

This chapter is built around `httpx` and the `pyfly.client` package, both
of which ship with the framework. Everything here runs against PyFly
v26.6.110. You do not need to install anything new — if you have been
following along with Lumen, the client tooling is already on your path.

---

## Why split (and why it hurts)

### The monolith comfort zone

A monolith is not an architectural mistake — it is an architectural
starting point. Lumen began as one service because one service was right:
one team, one deployment pipeline, one set of concerns to reason about.
The database transaction that writes a wallet row and publishes a domain
event in the same unit of work was not a compromise; it was the optimal
choice.

The pressure to split usually arrives from outside the architecture.
Payments needs a separate compliance audit trail. Risk scoring needs a
specialist team with access to a private data source. Settlement
processing demands throughput an order of magnitude higher than balance
reads. Any one of these is a good reason to extract a service — and none
of them erases the fact that the rest of the system still needs to call
the extracted service across a network boundary.

### The cost of the network

Network calls fail in ways that local function calls do not. A method on
a local object either returns a value or raises an exception. An HTTP call
to a remote service can time out (the remote is slow), refuse the
connection (the remote is down), return a transient 503 (the remote is
overloaded), or succeed only on the third attempt. In a monolith these
failure modes are irrelevant; in a distributed system they are your
baseline.

The naive fix — use `httpx` directly with `try/except` around every call
— works for one call site but does not scale. You end up with circuit-
breaker logic duplicated across every service client, retry delays
hardcoded in handlers, and timeout values scattered through `pyfly.yaml`
fragments that nobody owns. When Payments introduces a new endpoint, every
caller must remember to add all the resilience scaffolding again.

PyFly's typed HTTP client eliminates that duplication. You declare what
the remote service looks like — its endpoints, paths, and parameter
shapes. PyFly generates the implementation at startup, wires in a circuit
breaker and retry policy from `pyfly.yaml`, and registers the bean in the
container so any handler that needs it can declare it as a constructor
argument. Resilience is applied once, consistently, in the right layer.

---

## A typed service client

### Declarative over imperative

The core insight behind PyFly's client module is that service-to-service
contracts are better expressed as types than as procedural HTTP logic. When
you describe `PaymentsClient` as a class with typed method signatures, you
get a Python interface that any IDE can navigate, any type checker can
verify, and any test can mock — without ever importing `httpx` in the code
that uses it.

Two decorators define that contract:

| Decorator | Resilience built in | Use for |
|---|---|---|
| `@service_client` | Circuit breaker + retry | Production service-to-service calls |
| `@http_client` | None | Lightweight clients, testing, internal tooling |

Use **`@service_client`** whenever the target is another microservice.
Reserve `@http_client` for internal utilities and test doubles.

### Defining the Payments client

The Payments service exposes two endpoints: one to create a payment
instruction and one to retrieve a payment by identifier. Defining the
client means writing the class. Let us build it one decision at a time.

**Step 1 — Create the file.** Add `src/lumen/sdk/payments_client.py`
alongside the existing `client.py`. The `sdk` package is where Lumen keeps
the code that *talks to* services, so this is the natural home for a
service client.

**Step 2 — Decorate the class.** Put `@service_client(base_url=...)` on a
plain class. That single decorator is what turns an ordinary class into a
PyFly-managed HTTP client: it records the base URL, switches on the
resilience features, and registers the class as a bean so the container
can inject it later.

**Step 3 — Declare one method per endpoint.** Each method gets a verb
decorator (`@post`, `@get`, `@patch`, `@delete`) carrying the path. The
method body is just `...` — an *ellipsis*, Python's literal for "nothing
here yet." You never write the request code; PyFly writes it for you at
startup.

The full client looks like this.

::: figure art/figures/11-client.svg | Figure 11.1 — The PyFly declarative client pipeline. You write the interface; HttpClientBeanPostProcessor generates the implementation.

::: listing lumen/sdk/payments_client.py | Listing 11.1 — Typed Payments client with @service_client
from __future__ import annotations

from pyfly.client import (
    delete,
    get,
    patch,
    post,
    service_client,
)


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
    circuit_breaker_failure_threshold=5,
    circuit_breaker_recovery_timeout=60.0,
    retry_base_delay=1.0,
)
class PaymentsClient:
    """Typed HTTP client for the Payments service.

    Method stubs are replaced with real HTTP implementations by
    HttpClientBeanPostProcessor at application startup. Declare
    this class as a constructor argument to have it injected.
    """

    @post("/payments")
    async def create_payment(self, body: dict) -> dict:
        """POST /payments — submit a payment instruction."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(self, payment_id: str) -> dict:
        """GET /payments/:payment_id — fetch a payment by ID."""
        ...

    @patch("/payments/{payment_id}/cancel")
    async def cancel_payment(self, payment_id: str) -> dict:
        """PATCH /payments/:payment_id/cancel — cancel pending."""
        ...

    @delete("/payments/{payment_id}")
    async def delete_payment(self, payment_id: str) -> None:
        """DELETE /payments/:payment_id — remove a completed record."""
        ...
:::

**How it works — the declaration pipeline:**

`@service_client(base_url=...)` stamps metadata attributes on the class
and registers it as a singleton bean in the PyFly container — the same
`__pyfly_injectable__ = True` mechanism that `@service` uses. The
`base_url` is stored as `__pyfly_http_base_url__`; the resilience
options land in `__pyfly_resilience__`.

The verb decorators — `@post("/payments")`, `@get("/payments/{payment_id}")`,
and the others — each attach two attributes to their method:
`__pyfly_http_method__` (the HTTP verb string) and `__pyfly_http_path__`
(the path template). The method body itself becomes a stub that raises
`NotImplementedError` and should never be called directly.

At startup, `HttpClientBeanPostProcessor.after_init()` inspects every bean.
When it finds a class with `__pyfly_http_client__ = True`, it creates an
`HttpxClientAdapter` for `base_url`, scans every method for
`__pyfly_http_method__`, and replaces each stub with a real async
implementation. That implementation uses `inspect.signature()` to bind the
caller's arguments, interpolates path variables (`{payment_id}` → the
actual value), separates remaining parameters into query strings or a JSON
body, and calls `client.request()`. Responses with status ≥ 400 raise typed
exceptions; successful responses return `response.json()`.

Path variable interpolation is positional: any parameter whose name matches
a `{placeholder}` in the path template is substituted. For
`get_payment(self, payment_id: str)`, calling `client.get_payment("pay-123")`
sends `GET /payments/pay-123`. For `create_payment(self, body: dict)`,
calling `client.create_payment({"amount": 5000})` sends `POST /payments` with
the dict serialised as the JSON body. Parameters named `body` on
POST/PUT/PATCH methods are always treated as the JSON request body; all other
non-path parameters on GET/DELETE become query-string parameters.

!!! note "What just happened"
    You wrote four method *signatures* and zero lines of HTTP code. The
    `@service_client` decorator tagged the class for the container; the
    verb decorators tagged each method with a verb and a path. At startup,
    a behind-the-scenes component called `HttpClientBeanPostProcessor`
    reads those tags and quietly replaces every `...` stub with a working
    async method that builds the URL, sends the request, and turns the
    response back into Python. From the caller's point of view,
    `await client.get_payment("pay-123")` looks exactly like calling a
    local method — the network is hidden.

**Run it — confirm the stubs are wired.** Until the application context
starts, those `...` bodies raise `NotImplementedError` on purpose, so you
cannot just call the method in a bare script. The honest way to prove the
client works is a tiny test that starts a context (or wires the
post-processor) and inspects the generated method. The quickest smoke
check is to confirm the metadata the decorators stamped on:

```
uv run python -c "from lumen.sdk.payments_client import PaymentsClient; \
print(PaymentsClient.__pyfly_http_base_url__); \
print(PaymentsClient.create_payment.__pyfly_http_method__, \
PaymentsClient.create_payment.__pyfly_http_path__)"
```

Expected output:

```
http://payments-service:8080
POST /payments
```

If you see the base URL and `POST /payments`, the decorators applied
correctly and the post-processor has everything it needs to generate the
real implementation when the app boots.

!!! spring "Spring parity"
    `@service_client` with `@get`/`@post`/`@put`/`@delete`/`@patch` is
    PyFly's counterpart of Spring Cloud OpenFeign's `@FeignClient` with
    `@GetMapping`/`@PostMapping` etc. In Feign you annotate an interface;
    in PyFly you annotate a class with stub methods — the intent is
    identical. Both frameworks generate the HTTP implementation at startup
    time, inject the bean through the DI container, and support circuit
    breakers (Feign via Resilience4j; PyFly via the built-in
    `CircuitBreaker`). The key difference is that Feign works on Java
    interfaces while PyFly works on ordinary Python classes, which means
    you can add helper methods alongside the stub methods — useful for
    response-shaping logic that belongs inside the client class itself.

### Injecting the client into a handler

Because `PaymentsClient` is a singleton bean, any `@service` or
`@command_handler` can declare it as a constructor argument. PyFly's
container injects it through the same autowiring path used for
repositories and domain services.

!!! note "Bean and autowiring, briefly"
    A **bean** is just an object the framework creates and manages for you
    — you never call its constructor yourself. **Autowiring** is how the
    container hands a bean to whatever needs it: when a class lists
    `payments: PaymentsClient` in its `__init__`, PyFly notices the type,
    finds the matching bean, and passes it in automatically. You declare
    the dependency by *type*; the container does the lookup.

Lumen's Wallet service already applies the `WalletRepository` and `Money`
value object pattern from earlier chapters. When the wallet must call
Payments to settle a withdrawal, the handler follows that same pattern in
three steps: load the wallet, withdraw through the aggregate, then call the
external service.

**Step 1 — Declare the dependency.** Add `payments: PaymentsClient` to the
handler's constructor and stash it on `self`. That is the only wiring you
write; the container supplies the live client.

**Step 2 — Do the local work first.** Load the wallet and call
`wallet.withdraw(...)`, then persist it, so the wallet's own state is
settled before any network call happens.

**Step 3 — Call the remote service.** `await self._payments.create_payment(...)`
reads like a local method call. The resilience and HTTP details are
already baked into the injected client.

Here is the handler.

::: listing lumen/core/services/wallets/settle_transfer_handler.py | Listing 11.2 — CommandHandler injecting PaymentsClient
from __future__ import annotations

from lumen.core.services.wallets.settle_transfer_command import (
    SettleTransfer,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from lumen.sdk.payments_client import PaymentsClient
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound


@command_handler
@service
class SettleTransferHandler(CommandHandler[SettleTransfer, dict]):
    """Withdraw from the wallet and submit a payment instruction."""

    def __init__(
        self,
        repository: WalletRepository,
        payments: PaymentsClient,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.withdraw(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.add(wallet)

        payment = await self._payments.create_payment({
            "wallet_id": command.wallet_id,
            "amount": command.amount,
            "currency": wallet.currency.value,
            "reference": command.reference,
        })
        return payment
:::

**How it works — the injection path:**

`payments: PaymentsClient` in the constructor is resolved by the
container at startup. `HttpClientBeanPostProcessor` wires `PaymentsClient`
before `SettleTransferHandler` is instantiated, so the injected bean is
fully operational. The handler calls `await self._payments.create_payment(...)`
exactly as if it were a local async method. Connection pooling, header
propagation, and error mapping are all invisible to the handler.

`wallet.withdraw(Money(...))` runs before the network call, so the wallet
state is committed before Payments is contacted. If Payments is
temporarily unavailable, the retry and circuit breaker — described in the
next section — handle recovery transparently, without any code in the
handler.

**Run it — exercise the handler with a fake client.** Because the handler
depends on the *type* `PaymentsClient`, you can substitute a stand-in in a
test without any network. Drop this into `tests/test_settle_transfer.py`
and run it:

```
uv run --extra dev pytest tests/test_settle_transfer.py -q
```

A passing run prints something like:

```
1 passed in 0.12s
```

The point of the test is the substitution: you pass a hand-made object in
place of the real `PaymentsClient`, assert the handler called
`create_payment` with the expected body, and never open a socket. That is
the practical payoff of declaring the dependency by type — the handler
neither knows nor cares whether the client on the other end is real or
faked.

---

## Resilience on the wire

### Why the client layer is the right place for resilience

Resilience logic inside a handler mixes business concerns with
infrastructure plumbing. A handler that catches `httpx.ConnectError` and
implements its own backoff loop is doing two things at once: settling a
transfer *and* managing HTTP failure modes. Those responsibilities belong
in separate layers.

**`@service_client`** moves the **circuit breaker** and **retry policy** to
the client layer, where they belong. You configure them once on the
decorator, and every method on the client inherits them uniformly. The
handler code stays focused on the business operation.

### Circuit breaker

A circuit breaker monitors every call to the remote service. When
`failure_threshold` consecutive calls fail, the circuit **opens**: subsequent
calls are rejected immediately with `CircuitBreakerException` rather than
waiting for a timeout. This prevents a single slow or unavailable service
from blocking the event loop and exhausting connection pools across every
caller.

After `circuit_breaker_recovery_timeout` seconds, the circuit enters
**half-open**: one probe request is admitted. If it succeeds, the circuit
closes and normal operation resumes. If it fails, the circuit re-opens and
the recovery timer resets.

`@service_client` wires the breaker automatically. If you need it
standalone:

::: listing lumen/sdk/standalone_breaker.py | Listing 11.3 — Using CircuitBreaker standalone
from __future__ import annotations

from datetime import timedelta

from pyfly.client import CircuitBreaker
from pyfly.kernel.exceptions import CircuitBreakerException


breaker = CircuitBreaker(
    failure_threshold=3,
    recovery_timeout=timedelta(seconds=30),
)


async def call_with_breaker(client, payment_id: str) -> dict:
    """Fetch a payment through a standalone circuit breaker."""
    try:
        return await breaker.call(
            client.get_payment,
            payment_id,
        )
    except CircuitBreakerException:
        return {"status": "unavailable", "payment_id": payment_id}
:::

**How it works:** `CircuitBreaker.__init__` accepts `failure_threshold`
(default `5`) and `recovery_timeout` as a `timedelta` (default 30 s).
`breaker.call(func, *args)` executes `func(*args)` inside the breaker:
on success it resets the failure count; on failure it increments the
count and flips the state to `OPEN` once the threshold is reached. The
state transitions `CLOSED → HALF_OPEN` are computed lazily with
`time.monotonic()` — there is no background timer.

`CircuitBreakerException` is never counted as a failure. It signals that
the circuit is already open, so re-raising it without recording another
failure prevents the recovery timeout from resetting indefinitely.

!!! note "Three states, in plain language"
    Think of the breaker as a light switch with three positions.
    **Closed** is normal — calls flow through. **Open** means "stop
    trying" — calls fail instantly without touching the network, which
    spares your service from waiting on something that is clearly down.
    **Half-open** is "let me test the water" — after the recovery timeout,
    the breaker lets exactly one call through; if it works, the switch
    goes back to closed, and if it fails, it snaps open again.

**Run it — watch a breaker open and recover.** You can drive the breaker
by hand from a REPL with no real service involved. Start one with
`uv run python` from `samples/lumen` and try:

```
uv run python -c "
import asyncio
from datetime import timedelta
from pyfly.client import CircuitBreaker, CircuitState
from pyfly.kernel.exceptions import CircuitBreakerException

async def boom():
    raise RuntimeError('payments down')

async def main():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=timedelta(seconds=30))
    for _ in range(2):
        try:
            await cb.call(boom)
        except RuntimeError:
            pass
    print('state after 2 failures:', cb.state.name)
    try:
        await cb.call(boom)
    except CircuitBreakerException:
        print('open circuit rejected the call without calling boom')

asyncio.run(main())
"
```

Expected output:

```
state after 2 failures: OPEN
open circuit rejected the call without calling boom
```

The second line is the whole point: once the circuit is open, the breaker
raises `CircuitBreakerException` *immediately* instead of running `boom`
again. Drop `recovery_timeout` to `timedelta(seconds=0)` and re-run — the
next read of `cb.state` reports `HALF_OPEN` and the third call admits a
probe (so `boom` runs again) rather than being rejected. That is the
breaker letting the service prove it has recovered.

### Retry policy

Transient failures — a momentary latency spike, a rolling restart, a
brief connection reset — do not need a circuit breaker; they need a second
attempt. `RetryPolicy` provides exponential backoff with configurable
exception filtering:

::: listing lumen/sdk/standalone_retry.py | Listing 11.4 — Using RetryPolicy standalone
from __future__ import annotations

from datetime import timedelta

from pyfly.client import RetryPolicy


policy = RetryPolicy(
    max_attempts=3,
    base_delay=timedelta(milliseconds=500),
    retry_on=(ConnectionError, TimeoutError),
)


async def resilient_fetch(client, payment_id: str) -> dict:
    """Fetch a payment with retry on transient network errors."""
    return await policy.execute(
        client.get_payment,
        payment_id,
    )
:::

**How it works:** `RetryPolicy.__init__` accepts `max_attempts` (default
3, counting the first attempt), `base_delay` (default 1 s), and
`retry_on` — a tuple of exception types. The backoff formula is
`base_delay * (2 ** attempt)`: for `base_delay=0.5 s`, the delays are
0.5 s, 1 s, 2 s. Only exceptions matching `retry_on` trigger a retry;
others propagate immediately. This matters: you do not want to retry a
404 (the resource does not exist) or a 422 (the request is semantically
invalid).

!!! note "Backoff, in plain language"
    *Backoff* means waiting a little longer before each retry instead of
    hammering the remote service the instant it fails. *Exponential*
    backoff doubles the wait each time, so a service that needs a moment
    to recover gets more breathing room with every attempt while a healthy
    one is retried almost immediately.

**Run it — see retry recover from a transient error.** Simulate a call
that fails twice and then succeeds:

```
uv run python -c "
import asyncio
from datetime import timedelta
from pyfly.client import RetryPolicy

attempts = {'n': 0}
async def flaky():
    attempts['n'] += 1
    if attempts['n'] < 3:
        raise ConnectionError('reset')
    return 'ok'

async def main():
    policy = RetryPolicy(max_attempts=3, base_delay=timedelta(milliseconds=1),
                         retry_on=(ConnectionError,))
    print('result:', await policy.execute(flaky))
    print('attempts:', attempts['n'])

asyncio.run(main())
"
```

Expected output:

```
result: ok
attempts: 3
```

Three attempts, one success. Change `retry_on` to `(TimeoutError,)` and
the very first `ConnectionError` propagates instead — proof that only the
exceptions you list are retried.

When `@service_client` enables both features, the post-processor wraps
them in the correct order: circuit breaker *outside*, retry *inside*. A
single logical call attempts up to `max_attempts` retries before the
circuit breaker records one failure. An open circuit rejects the call
immediately, bypassing the retry loop entirely.

### Typed error exceptions

When the remote service returns a 4xx or 5xx response, the generated
method raises a typed exception instead of returning the error payload as
if it were a success. The exception hierarchy lives in
`pyfly.client.exceptions` — import the classes you want to catch from
there (for example,
`from pyfly.client.exceptions import ServiceNotFoundException`):

| Status | Exception class | `retryable` |
|---|---|---|
| 400 | `ServiceValidationException` | False |
| 401 / 403 | `ServiceAuthenticationException` | False |
| 404 | `ServiceNotFoundException` | False |
| 409 | `ServiceConflictException` | False |
| 422 | `ServiceUnprocessableEntityException` | False |
| 429 | `ServiceRateLimitException` | True |
| 5xx | `ServiceUnavailableException` | True |

All exceptions extend `ServiceClientException` (itself an
`InfrastructureException`). The `retryable` flag on
`ServiceRateLimitException` and `ServiceUnavailableException` tells the
post-processor which exceptions to pass to the retry policy. 4xx
validation errors and 404s are never retried.

!!! note "What just happened"
    The three resilience pieces fit together like nested boxes. The
    **typed exceptions** classify *what kind* of failure occurred — a 404
    is not retryable, a 503 is. The **retry policy** uses that
    classification to decide whether to try again. The **circuit breaker**
    sits outside the retry loop and counts sustained failures so it can
    stop calling a service that is genuinely down. You did not write any
    of this glue: `@service_client` assembled it the moment you set
    `circuit_breaker=True` and `retry=3`.

### Configuring defaults in pyfly.yaml

Per-service overrides on `@service_client` always take precedence.
Setting process-wide defaults in `pyfly.yaml` lets new clients inherit
sensible values without repeating them on every decorator:

::: listing pyfly.yaml | Listing 11.5 — Client resilience defaults in pyfly.yaml
pyfly:
  client:
    timeout: 10
    retry:
      max-attempts: 3
      base-delay: 1.0
    circuit-breaker:
      failure-threshold: 5
      recovery-timeout: 30
:::

| Key | Description | Default |
|---|---|---|
| `pyfly.client.timeout` | Request timeout in seconds | `30` |
| `pyfly.client.retry.max-attempts` | Total attempts including first | `3` |
| `pyfly.client.retry.base-delay` | Base delay in seconds | `1.0` |
| `pyfly.client.circuit-breaker.failure-threshold` | Consecutive failures to open | `5` |
| `pyfly.client.circuit-breaker.recovery-timeout` | Seconds before probing | `30` |

`ClientAutoConfiguration` reads `pyfly.client.timeout` at startup and
passes it to `HttpxClientAdapter`. The `retry` and `circuit-breaker`
sub-maps are forwarded as `default_retry` and `default_circuit_breaker`
to `HttpClientBeanPostProcessor`. Any value set directly on
`@service_client(circuit_breaker_failure_threshold=...)` overrides the
default.

!!! note "Precedence, in plain language"
    Two layers can set these knobs: the global `pyfly.yaml` defaults and
    the per-client decorator arguments. The decorator always wins. Think
    of `pyfly.yaml` as the house style every new client inherits, and the
    decorator as the place to override that style for one demanding
    upstream.

**Run it — confirm the config is read.** After adding the block to
`pyfly.yaml`, read the values back through PyFly's `Config` to be sure the
keys are spelled correctly (a common cause of "my timeout is being
ignored" is a typo). Run this from the project root, where `pyfly.yaml`
lives:

```
uv run python -c "
from pyfly.core.config import Config
cfg = Config.from_sources('.')
print('timeout:', cfg.get('pyfly.client.timeout'))
print('cb:', cfg.get('pyfly.client.circuit-breaker'))
"
```

Expected output once Listing 11.5 is in place:

```
timeout: 10
cb: {'failure-threshold': 5, 'recovery-timeout': 30}
```

Before you add the block, `timeout` reports `30` — the framework default
from `pyfly-defaults.yaml` — which confirms the override is what changed
it. If a key comes back as `None`, check the indentation in `pyfly.yaml`:
YAML nesting is whitespace-sensitive, and a misaligned `circuit-breaker:`
silently lands under the wrong parent.

!!! tip "Set per-service timeouts low"
    The default `timeout: 30` is conservative. In production, each
    service should carry a `pyfly.yaml` override tuned to its SLA. A
    payments call that should complete in 500 ms should have `timeout: 2`
    — not 30 s — so a slow Payments instance fails fast and the circuit
    breaker can open before threads pile up.

---

## Auth, discovery, and deduplication

### Propagating identity downstream

When the Wallet service calls Payments, it often needs to carry the
caller's identity — a JWT or an internal service token — so that Payments
can enforce its own authorisation rules. The `headers` parameter is
treated specially by the post-processor: when a stub method declares
`headers: dict`, the value is forwarded as HTTP request headers, not
serialised as a query string.

!!! note "JWT, in plain language"
    A **JWT** (JSON Web Token) is a signed string that travels in the
    `Authorization` header and proves who the caller is. When Wallet
    forwards the caller's JWT to Payments, Payments can re-check it and
    apply its own rules — the identity is carried across the network
    boundary rather than re-established from scratch.

To forward headers, you add a single optional parameter. There is no new
decorator and no special configuration:

- **Add `headers: dict | None = None` to the method.** The name `headers`
  is the magic word — the post-processor recognises it and routes its
  value to HTTP headers instead of the query string.
- **Pass a dict at the call site.** The handler supplies
  `headers={"Authorization": f"Bearer {token}"}`, and PyFly attaches it to
  the outgoing request.

::: listing lumen/sdk/payments_client_auth.py | Listing 11.6 — Forwarding auth headers per-call
from __future__ import annotations

from pyfly.client import get, post, service_client


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
)
class AuthenticatedPaymentsClient:
    """Payments client that forwards caller identity on each request."""

    @post("/payments")
    async def create_payment(
        self,
        body: dict,
        headers: dict | None = None,
    ) -> dict:
        """POST /payments — body is the JSON payload; headers forwarded."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(
        self,
        payment_id: str,
        headers: dict | None = None,
    ) -> dict:
        """GET /payments/:payment_id — headers are forwarded."""
        ...
:::

**How it works:** The post-processor checks whether a parameter named
`headers` is present in the bound arguments and is a `dict`. When both
conditions hold, it extracts the value from the query-parameter pool and
forwards it as HTTP request headers. The handler passes the incoming
`Authorization` header (or a freshly minted service token) as
`headers={"Authorization": f"Bearer {token}"}`.

`HttpxClientAdapter` also calls `inject_headers(headers)` on every
request, propagating the W3C `traceparent` and `tracestate` headers from
the current observability context so distributed traces stitch across
service boundaries without any application-level work.

!!! note "Service-to-service identity patterns"
    For internal services on a trusted network, a shared secret in an
    `X-Internal-Token` header is the simplest approach. For zero-trust
    architectures, consider mTLS (mutual TLS at the infrastructure layer)
    or a service mesh that injects identity certificates. For
    user-delegated calls, forward the original JWT. Whatever pattern you
    choose, the `headers` parameter gives you a clean injection point in
    the declarative client.

### Service discovery

When `base_url` is a static string like `http://payments-service:8080`,
you rely on DNS-based discovery — a Kubernetes `Service` or a Consul
record resolves `payments-service` to the correct cluster IP. This is the
recommended starting point and sufficient for most deployments.

For environments that need dynamic URL resolution (multiple environments
behind the same client class, feature-flagged routing), supply the URL
via configuration instead:

::: listing pyfly.yaml | Listing 11.7 — Per-environment base URL in pyfly.yaml
pyfly:
  client:
    timeout: 10

services:
  payments:
    base-url: "${PAYMENTS_SERVICE_URL:http://payments-service:8080}"
:::

A thin factory bean reads the config key and constructs the
post-processor with a custom factory that injects the resolved URL. The
client class itself does not change — only the factory does.

### Request deduplication

Financial operations must be idempotent at the HTTP layer. If
`create_payment` is called, times out, and is retried, Payments must not
create two payment records. The standard mechanism is an **`Idempotency-Key`**
header: a stable, caller-chosen identifier — typically the command's UUID —
that Payments uses to detect and deduplicate repeated requests.

::: listing lumen/core/services/wallets/settle_transfer_idempotent.py | Listing 11.8 — Idempotency-Key forwarded via headers parameter
from __future__ import annotations

from lumen.core.services.wallets.settle_transfer_command import (
    SettleTransfer,
)
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from lumen.sdk.payments_client_auth import AuthenticatedPaymentsClient
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound


@command_handler
@service
class SettleTransferIdempotentHandler(
    CommandHandler[SettleTransfer, dict]
):
    """Withdraw funds and submit payment with idempotency key."""

    def __init__(
        self,
        repository: WalletRepository,
        payments: AuthenticatedPaymentsClient,
    ) -> None:
        super().__init__()
        self._repository = repository
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.withdraw(
            Money(amount=command.amount, currency=wallet.currency)
        )
        await self._repository.add(wallet)

        idempotency_key = str(command.transfer_id)
        return await self._payments.create_payment(
            body={
                "wallet_id": command.wallet_id,
                "amount": command.amount,
                "currency": wallet.currency.value,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
:::

**How it works:** `command.transfer_id` is the stable identifier for
this business operation, determined before the command reaches the
handler. If the handler is called again for the same command — from a
retry, a re-delivery, or a dead-letter replay — it passes the same
`Idempotency-Key`. Payments stores the key alongside the created payment
record and returns the existing record when the key has been seen before,
rather than creating a second payment. That deduplication is a server-side
concern; the client's job is simply to forward the key consistently.

---

## The experience tier: the BFF

### Why the frontend cannot talk to both services directly

When a mobile app or web frontend needs to display a wallet summary that
includes pending payment instructions, it faces a choice: call Wallet for
the balance, call Payments for the pending list, and merge the results in
the client — or talk to a single API that does the merging server-side.
The first option incurs two round trips, exposes each service's internal
shape to the client, and forces the client to implement retry and error
handling for two independent failure domains. The second option is the
**BFF pattern**.

A **Backend for Frontend** is a lightweight service in the *experience
tier* that composes responses from multiple domain services into a shape
tailored to a specific frontend's needs. It handles response aggregation,
field renaming to match client conventions, and caching of composed
results. It never touches the database directly — it depends entirely on
domain service clients.

### Building the Lumen BFF

The Lumen SDK already ships a `LumenClient` in `lumen/sdk/client.py` that
wraps a raw `httpx.AsyncClient` — it takes an `httpx.AsyncClient` in its
constructor and calls `self._http.get(...)`/`self._http.post(...)` by hand,
raising on error with `response.raise_for_status()`. That is the
*imperative* style: useful, explicit, and entirely your responsibility to
keep resilient. In the BFF tier you use PyFly's declarative
`@service_client` instead — the calling code looks the same, but the
circuit breaker and retry are built in automatically.

We will build the BFF in four small pieces, each its own file:

**Step 1 — A `WalletClient`** that knows how to reach the Wallet service.
**Step 2 — A `PaymentsClient`** with one extra endpoint the BFF needs.
**Step 3 — A `WalletSummaryService`** that calls both and merges the
results. **Step 4 — A thin controller** that exposes the merged view.

Start with the wallet-side client.

::: listing lumen_bff/sdk/wallet_client.py | Listing 11.9 — WalletClient for the BFF tier
from __future__ import annotations

from pyfly.client import get, service_client


@service_client(
    base_url="http://wallet-service:8080",
    circuit_breaker=True,
    retry=3,
)
class WalletClient:
    """Typed HTTP client for the Lumen Wallet service."""

    @get("/api/v1/wallets/{wallet_id}")
    async def get_wallet(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id — fetch a wallet."""
        ...

    @get("/api/v1/wallets/{wallet_id}/balance")
    async def get_balance(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id/balance — current balance."""
        ...
:::

The paths mirror the real Lumen controller — `@request_mapping("/api/v1/wallets")` with `@get_mapping("/{wallet_id}")` — so the BFF client matches exactly what the Wallet service exposes.

The Payments service needs a `list_pending` endpoint that lets the BFF
query pending records by wallet:

::: listing lumen_bff/sdk/payments_client_bff.py | Listing 11.10 — Extended PaymentsClient for the BFF
from __future__ import annotations

from pyfly.client import get, post, service_client


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
)
class PaymentsClient:
    """Typed HTTP client for Payments (BFF edition)."""

    @post("/payments")
    async def create_payment(self, body: dict) -> dict:
        """POST /payments — submit a payment instruction."""
        ...

    @get("/payments/{payment_id}")
    async def get_payment(self, payment_id: str) -> dict:
        """GET /payments/:payment_id — fetch a payment by ID."""
        ...

    @get("/payments")
    async def list_pending(self, wallet_id: str) -> list:
        """GET /payments?wallet_id=... — list payments for a wallet."""
        ...
:::

The BFF service then composes the wallet balance with the pending payments
list into a single response.

!!! note "asyncio.gather, in plain language"
    `asyncio.gather(a, b)` starts coroutines `a` and `b` at the same time
    and waits for both to finish, returning their results as a list. For a
    BFF this means two upstream calls overlap instead of queuing one after
    the other. Adding `return_exceptions=True` changes one thing: instead
    of the whole `gather` blowing up when one call fails, the failed call's
    exception is handed back as that call's *result*, so you can inspect it
    and still use the successful one.

::: listing lumen_bff/application/bff_service.py | Listing 11.11 — BFF service composing Wallet + Payments
from __future__ import annotations

import asyncio

from lumen_bff.sdk.payments_client_bff import PaymentsClient
from lumen_bff.sdk.wallet_client import WalletClient
from pyfly.container import service


@service
class WalletSummaryService:
    """Composes wallet balance and pending payments into one view.

    Calls both domain services concurrently using asyncio.gather so the
    total latency is max(wallet_latency, payments_latency) rather than
    their sum.
    """

    def __init__(
        self,
        wallet: WalletClient,
        payments: PaymentsClient,
    ) -> None:
        self._wallet = wallet
        self._payments = payments

    async def get_summary(self, wallet_id: str) -> dict:
        """Return a unified summary for the given wallet."""
        wallet_data, pending = await asyncio.gather(
            self._wallet.get_wallet(wallet_id),
            self._payments.list_pending(wallet_id),
            return_exceptions=True,
        )

        balance_minor: int = 0
        if isinstance(wallet_data, dict):
            balance_minor = wallet_data.get("balance_minor", 0)

        pending_list: list = []
        if isinstance(pending, list):
            pending_list = pending

        return {
            "wallet_id": wallet_id,
            "balance_minor": balance_minor,
            "pending_payments": pending_list,
        }
:::

**How it works — the composition pattern:**

`asyncio.gather(...)` fires both upstream calls concurrently. The wallet
and payments calls run in parallel, so the composite latency is bounded
by the slower of the two rather than their sum — at 50 ms per service,
sequential calls cost 100 ms while concurrent calls cost roughly 55 ms.

`return_exceptions=True` is critical for a BFF. Without it, a single
upstream failure raises an exception and the caller receives nothing. With
it, a failed coroutine returns its exception object as the result instead
of propagating it. The service inspects each result with
`isinstance(wallet_data, dict)` and degrades gracefully — returning a
partial response with a zero balance or an empty payment list rather than
an HTTP 500. The BFF should make that decision explicit in its response
shape, for example by including an `"errors"` key listing degraded fields.

The field name `balance_minor` follows Lumen's convention: amounts are
stored as integer minor units (cents) and the field is named
`balance_minor` throughout — in `WalletDto`, in deposit/withdraw
responses, and here in the BFF summary.

Each `@service_client` wrapper on `WalletClient` and `PaymentsClient`
handles retries and circuit breaking for its upstream call independently.
If Payments is circuit-open, the wallet balance still appears; only the
pending payments list is empty.

### The BFF controller

The BFF exposes its composed response through a standard PyFly web handler.
The controller is intentionally thin — its sole job is to delegate to the
service:

::: listing lumen_bff/web/controllers/summary_controller.py | Listing 11.12 — BFF controller
from __future__ import annotations

from lumen_bff.application.bff_service import WalletSummaryService
from pyfly.container import rest_controller
from pyfly.web import get_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletSummaryController:
    """Experience-tier controller for the wallet summary view."""

    def __init__(self, summary: WalletSummaryService) -> None:
        self._summary = summary

    @get_mapping("/{wallet_id}/summary")
    async def get_wallet_summary(self, wallet_id: str) -> dict:
        """GET /api/v1/wallets/:wallet_id/summary"""
        return await self._summary.get_summary(wallet_id)
:::

**How it works:** The BFF controller imports no domain models and touches
no repositories — it depends only on `WalletSummaryService`, which in
turn depends only on typed client interfaces. The dependency chain is
controller → BFF service → declarative clients → remote HTTP. Each layer
is independently testable: the controller with a mock service, the
service with mock clients, and the clients with a mock `HttpClientPort`.

**Run it — call the composed endpoint.** With the BFF application running
(`uv run pyfly run --server uvicorn`) and the Wallet and Payments services
reachable, hit the summary route with a wallet id you have already
created:

```
curl -s http://localhost:8080/api/v1/wallets/wal-123/summary
```

Expected shape (values depend on your data):

```
{"wallet_id": "wal-123", "balance_minor": 5000, "pending_payments": []}
```

The single response merges two upstream services. To see the graceful
degradation in action, stop the Payments service and call again — the
`balance_minor` still comes back from Wallet, and `pending_payments`
falls back to `[]` rather than the whole request returning a 500. That is
`return_exceptions=True` doing its job.

!!! note "Default app port"
    PyFly serves the application on `pyfly.server.port`, which defaults to
    `8080` (matching Spring's `server.port`). Override it with the
    `PYFLY_SERVER_PORT` environment variable or the `pyfly.server.port`
    config key. Note that the actuator and admin dashboard run on a
    *separate* management port — `pyfly.management.server.port`, default
    `9090` — so health and info endpoints never collide with your API
    routes.

!!! note "BFF scope and team ownership"
    A BFF is scoped to one frontend or one user journey — not one per
    microservice. Lumen might have a `lumen-mobile-bff` and a
    `lumen-web-bff`, each composing the same domain services but
    returning shapes optimised for their respective clients. The BFF is
    owned by the frontend team, not the domain team. Domain services
    expose stable contracts; BFFs adapt those contracts to client-specific
    shapes without coupling the domain services to any particular
    frontend's conventions.

!!! spring "Spring parity"
    The BFF pattern in PyFly mirrors the Spring Boot API Gateway / BFF
    approach where a thin Spring Boot application aggregates responses
    from multiple microservices. In the reactive Spring stack, `Mono.zip()`
    provides the same concurrent aggregation that `asyncio.gather()` does
    in Python. The `@FeignClient` in the BFF corresponds to
    `@service_client` in PyFly; the Spring `WebClient` approach of
    chaining `.flatMap()` calls corresponds to PyFly's
    `asyncio.gather()` + `isinstance` error handling. The team-ownership
    model — BFF owned by the frontend team, domain services owned by
    domain teams — is identical.

---

## What you built {.recap}

You started this chapter with a single Lumen service and finished with
an architecture that scales in multiple dimensions. Here is what changed
and why it matters.

**You extracted PaymentsService.** Payments now runs in its own process
with its own deployment pipeline and its own data store. Wallet handlers
know nothing about how Payments stores payment records or which database
engine it uses — all they see is the typed interface that `PaymentsClient`
exposes.

**You declared the client, not the implementation.** `@service_client`
with `@post`, `@get`, `@patch`, and `@delete` stubs gave you a typed
interface that IDEs navigate and type checkers verify.
`HttpClientBeanPostProcessor` generated the HTTP implementation at startup,
read the resilience configuration from `pyfly.yaml`, and registered the
bean for injection anywhere.

**You made the network resilient.** `circuit_breaker=True` and `retry=3`
on the decorator wrapped every method with a shared circuit breaker and
retry policy — circuit breaker outside, retry inside — so a sustained
Payments outage opens the circuit fast while transient errors recover
automatically. Typed exceptions (`ServiceNotFoundException`,
`ServiceUnavailableException`) give callers a clean signal without
exposing raw HTTP status codes.

**You introduced the BFF tier.** `WalletSummaryService` composes two
upstream calls with `asyncio.gather`, returns a partial response when one
service is degraded, and exposes a single contract to the frontend. The
BFF absorbs each domain service's independent release cycle and shields
the frontend from their internal shapes.

Three principles carry through the rest of Part IV:

- **Depend on the typed client, not on `httpx` directly.** The declaration
  is your contract; the implementation is a framework detail.
- **Resilience belongs in the client layer.** Configure it once on
  `@service_client`; every handler that uses the client inherits it.
- **BFFs compose; domain services provide.** Domain services own stable,
  fine-grained contracts; BFFs own the coarse-grained compositions that
  specific frontends need.

---

## Try it yourself {.exercises}

1. **Add a fourth endpoint and verify path interpolation.** Extend
   `PaymentsClient` with a `@get("/payments")` method that accepts
   `wallet_id: str` and `status: str = "pending"` as parameters. Call it
   from a test and assert that the generated HTTP request is
   `GET /payments?wallet_id=abc&status=pending`. Verify that changing the
   default to `status="completed"` and calling the method without a
   `status` argument sends `status=completed` in the query string.

2. **Test the BFF with degraded upstream services.** Write a unit test
   for `WalletSummaryService.get_summary` that mocks
   `WalletClient.get_wallet` to succeed and
   `PaymentsClient.list_pending` to raise `ServiceUnavailableException`
   (import it with
   `from pyfly.client.exceptions import ServiceUnavailableException`).
   Assert that the method returns a dict with the correct `balance_minor`
   and an empty `pending_payments` list — confirming that the
   partial-response fallback works and a single upstream failure does not
   propagate as an exception to the BFF's caller. Run it with
   `uv run --extra dev pytest tests/test_wallet_summary.py -q` and look
   for `1 passed`.

3. **Tune circuit breaker thresholds for a brittle upstream.** Suppose
   Payments has a known flakiness window during its nightly batch run:
   it returns 503 on roughly 20 % of requests for about 10 seconds
   before stabilising. Configure `PaymentsClient` with
   `circuit_breaker_failure_threshold=2` and
   `circuit_breaker_recovery_timeout=15.0` and write a test using a mock
   `HttpClientPort` that simulates two consecutive failures followed by
   success. Assert that the third call (the probe after the recovery
   timeout) succeeds and that the circuit transitions back to `CLOSED`.
