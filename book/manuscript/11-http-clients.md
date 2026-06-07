<span class="eyebrow">Chapter 11</span>

# Splitting the Monolith: HTTP Clients & the BFF {.chtitle}

::: figure art/openers/ch11.svg | &nbsp;

When Lumen was a single service, every capability lived in the same process. The wallet, the balance check, and the payment processing all ran together — simple to start, straightforward to test, and perfectly adequate until the team needed to release different features at different cadences. Then came the hard conversation about splitting.

The promise of a microservice split is real: teams can own their services independently, scale them separately, and deploy them without coordinating a shared release window. But every split creates a new problem that the monolith never had — the network. What was a local function call becomes an HTTP request that can time out, fail midway, or land on a service that is temporarily overwhelmed. The distance between services is not just a deployment detail; it is a first-class engineering concern.

This chapter introduces `PaymentsService`, Lumen's second service. The Wallet service must call it to settle transfers, and that call must be resilient. Rather than hand-rolling `httpx` sessions and threading circuit breaker logic through every handler, you will define the Payments client as a plain Python class — a typed, declarative interface that PyFly fills in at startup. By the end of the chapter you will also see how a BFF (Backend for Frontend) tier sits in front of both services to compose their capabilities into a single, user-journey-focused API.

---

## Why split (and why it hurts)

### The monolith comfort zone

A monolith is not an architectural mistake — it is an architectural starting point. Lumen began as one service because one service was right: there was one team, one deployment pipeline, and one set of concerns to reason about. The database transaction that writes a wallet row and publishes the domain event in the same unit of work was not a compromise; it was the optimal choice.

The pressure to split usually arrives from outside the architecture. Payments needs a different compliance audit trail. The risk-scoring logic needs a specialised team with access to a private data source. The throughput requirements for settlement processing are an order of magnitude higher than for balance reads. Any one of these is a good reason to extract a service. None of them changes the fact that the rest of the system still needs to call the extracted service — and now those calls cross a network boundary.

### The cost of the network

Network calls fail in ways that function calls do not. A method on a local object either returns a value or raises an exception. An HTTP call to a remote service can time out (the remote is slow), refuse the connection (the remote is down), return a transient 503 (the remote is overloaded), or succeed on the third attempt after two failed ones. In a monolith you never thought about these failure modes; in a distributed system they are your baseline.

The naive approach is to use `httpx` directly and add try/except around every call. This works for one call site, but it does not scale. You end up with circuit breaker logic duplicated across every service client, retry delays hardcoded in the handlers that call them, and timeout configurations scattered through `application.yaml` fragments that nobody owns. When Payments introduces a new endpoint, whoever calls it must remember to add all the resilience scaffolding again.

PyFly's typed HTTP client addresses this directly. You declare what the remote service looks like — its endpoints, their paths, and their parameter shapes. PyFly generates the implementation at startup, wires in a circuit breaker and a retry policy drawn from your `pyfly.yaml`, and registers the resulting bean in the container so any service that needs it can declare it as a constructor argument. The resilience is applied once, consistently, in the right layer.

---

## A typed service client

### Declarative over imperative

The core insight behind PyFly's client module is that service-to-service contracts are better expressed as types than as procedural HTTP logic. When you describe `PaymentsClient` as a class with typed method signatures, you get a Python interface that any IDE can navigate, any type checker can verify, and any test can mock — without ever importing `httpx` in the code that uses it.

Two decorators define that contract:

| Decorator | Resilience built in | Use for |
|---|---|---|
| `@service_client` | Circuit breaker + retry | Production service-to-service calls |
| `@http_client` | None | Lightweight clients, testing, internal tooling |

Prefer `@service_client` whenever the target is another microservice. Reserve `@http_client` for internal utilities and test doubles.

### Defining the Payments client

The Payments service exposes two endpoints: one to create a payment instruction and one to retrieve a payment by its identifier. Defining the client is a matter of writing the class:

::: figure art/figures/11-client.svg | Figure 11.1 — The PyFly declarative client pipeline. You write the interface; HttpClientBeanPostProcessor generates the implementation.

::: listing payments/client/payments_client.py | Listing 11.1 — Typed Payments client with @service_client
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
        """PATCH /payments/:payment_id/cancel — cancel a pending payment."""
        ...

    @delete("/payments/{payment_id}")
    async def delete_payment(self, payment_id: str) -> None:
        """DELETE /payments/:payment_id — remove a completed payment record."""
        ...
:::

**How it works — the declaration pipeline:**

`@service_client(base_url=...)` stamps four metadata attributes on the class and registers it as a singleton bean in the PyFly container (the same `__pyfly_injectable__ = True` mechanism that `@service` uses). The `base_url` is stored as `__pyfly_http_base_url__`; the resilience options land in `__pyfly_resilience__`.

`@post("/payments")`, `@get("/payments/{payment_id}")`, and the other verb decorators attach two attributes to each method: `__pyfly_http_method__` (the HTTP verb string) and `__pyfly_http_path__` (the path template). The method body itself is replaced with a placeholder that raises `NotImplementedError` — it is a stub that should never be called directly.

At application startup, `HttpClientBeanPostProcessor.after_init()` inspects every bean. When it finds a class with `__pyfly_http_client__ = True`, it creates an `HttpxClientAdapter` for the `base_url`, scans every method for `__pyfly_http_method__`, and replaces each stub with a real async implementation. That implementation uses `inspect.signature()` to bind the caller's arguments, interpolates path variables (`{payment_id}` → the actual value), separates remaining parameters into query strings or JSON body, and calls `client.request()`. The result is `response.json()` for successful responses, or a typed exception for 4xx/5xx.

Path variable interpolation is positional: any parameter whose name matches a `{placeholder}` in the path template is substituted. For `get_payment(self, payment_id: str)`, calling `client.get_payment("pay-123")` sends `GET /payments/pay-123`. For `create_payment(self, body: dict)`, calling `client.create_payment({"amount": 5000})` sends `POST /payments` with the dict serialized as the JSON request body. Parameters named `body` on POST/PUT/PATCH methods are always treated as the JSON request body; all other non-path parameters become query string parameters on GET/DELETE.

!!! spring "Spring parity"
    `@service_client` with `@get`/`@post`/`@put`/`@delete`/`@patch` is PyFly's counterpart of Spring Cloud OpenFeign's `@FeignClient` with `@GetMapping`/`@PostMapping` etc. In Feign you annotate an interface; in PyFly you annotate a class with stub methods — the intent is identical. Both frameworks generate the HTTP implementation at startup time, inject the bean through the DI container, and support circuit breakers (Feign via Resilience4j; PyFly via the built-in `CircuitBreaker`). The key difference is that Feign works on Java interfaces while PyFly works on ordinary Python classes, which means you can add helper methods alongside the stub methods — useful for response-shaping logic that belongs inside the client class itself.

### Injecting the client into a handler

Because `PaymentsClient` is a singleton bean, any `@service` or `@command_handler` can declare it as a constructor argument. PyFly's container injects it through the same autowiring mechanism that wires repositories and domain services:

::: listing wallet/application/handlers/settle_transfer_handler.py | Listing 11.2 — CommandHandler injecting PaymentsClient
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound

from payments.client.payments_client import PaymentsClient
from wallet.application.commands import SettleTransfer
from wallet.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class SettleTransferHandler(
    CommandHandler[SettleTransfer, dict]
):
    """Debit the wallet and submit a payment instruction."""

    def __init__(
        self,
        repo: WalletDomainRepository,
        payments: PaymentsClient,
    ) -> None:
        self._repo = repo
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repo.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.debit(command.amount_cents, command.currency)
        await self._repo.save(wallet)

        payment = await self._payments.create_payment({
            "wallet_id": command.wallet_id,
            "amount_cents": command.amount_cents,
            "currency": command.currency,
            "reference": command.reference,
        })
        return payment
:::

**How it works — the injection path:**

`payments: PaymentsClient` in the constructor is resolved by the container at startup. Before `SettleTransferHandler` is instantiated, `HttpClientBeanPostProcessor` has already wired `PaymentsClient` — so the injected bean is fully operational. The handler calls `await self._payments.create_payment(...)` as if it were a local async method. All the HTTP machinery — connection pooling, header propagation, error mapping — is invisible to the handler.

Notice that `wallet.debit(...)` and `repo.save(wallet)` run before the payment call. The wallet state is committed before the network call that depends on it. If Payments is temporarily unavailable, the retry and circuit breaker (described in the next section) handle the recovery transparently from the handler's perspective.

---

## Resilience on the wire

### Why the client layer is the right place for resilience

Resilience logic that lives inside a handler contaminates business logic with infrastructure concerns. A handler that catches `httpx.ConnectError` and implements its own backoff loop is doing two things: settling a transfer *and* managing HTTP failure modes. Those responsibilities belong in separate layers.

`@service_client` moves the circuit breaker and retry policy to the client layer, where they belong. You configure them once on the decorator, and every method on the client benefits uniformly. The handler code remains focused on the business operation.

### Circuit breaker

A circuit breaker watches every call to the remote service. When `failure_threshold` consecutive calls fail, the circuit *opens*: subsequent calls are rejected immediately with a `CircuitBreakerException` rather than waiting for a timeout to expire. This prevents a single slow or unavailable service from blocking threads and exhausting connection pools across every service that calls it.

After `circuit_breaker_recovery_timeout` seconds, the circuit enters the *half-open* state: one probe request is allowed through. If it succeeds, the circuit closes and normal operation resumes. If it fails, the circuit re-opens and the recovery timer resets.

`@service_client` wires the breaker for you. If you need it standalone:

::: listing payments/resilience/standalone_breaker.py | Listing 11.3 — Using CircuitBreaker standalone
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

**How it works:** `CircuitBreaker.__init__` accepts `failure_threshold` (default `5`) and `recovery_timeout` as a `timedelta` (default 30 s). `breaker.call(func, *args)` executes `func(*args)` inside the breaker. On success it resets the failure count. On failure it increments the count; if the count reaches the threshold the state flips to `OPEN`. The state property computes `CLOSED → HALF_OPEN` lazily using `time.monotonic()` — there is no background timer.

`CircuitBreakerException` itself is never counted as a failure: it is the signal that the circuit is already open, so re-raising it without recording another failure prevents the recovery timeout from being extended by the very exceptions the breaker is emitting.

### Retry policy

Transient failures — a momentary spike in latency, a rolling restart, a brief connection reset — do not need a circuit breaker; they need a second attempt. `RetryPolicy` provides exponential backoff with configurable exception filtering:

::: listing payments/resilience/standalone_retry.py | Listing 11.4 — Using RetryPolicy standalone
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

**How it works:** `RetryPolicy.__init__` accepts `max_attempts` (default 3, counting the first attempt), `base_delay` (default 1 s), and `retry_on` — a tuple of exception types. The backoff formula is `base_delay * (2 ** attempt)`: for `base_delay=0.5 s`, the delays are 0.5 s, 1 s, 2 s. Only exceptions matching `retry_on` trigger a retry. Exceptions outside the tuple propagate immediately — this matters because you do not want to retry a 404 (the resource does not exist) or a 422 (the request is semantically invalid).

When `@service_client` enables both resilience features, the post-processor wraps them in the correct order: circuit breaker *outside*, retry *inside*. A single logical call attempts up to `max_attempts` retries before the circuit breaker records one failure. An open circuit rejects the call immediately without entering the retry loop at all.

### Typed error exceptions

When the remote service returns a 4xx or 5xx response, the generated method raises a typed exception rather than returning the error payload as if it were a success. The exception hierarchy lives in `pyfly.client`:

| Status | Exception class | `retryable` |
|---|---|---|
| 400 | `ServiceValidationException` | False |
| 401 / 403 | `ServiceAuthenticationException` | False |
| 404 | `ServiceNotFoundException` | False |
| 409 | `ServiceConflictException` | False |
| 422 | `ServiceUnprocessableEntityException` | False |
| 429 | `ServiceRateLimitException` | True |
| 5xx | `ServiceUnavailableException` | True |

All exceptions extend `ServiceClientException` (which extends `InfrastructureException`). The `retryable` flag on `ServiceRateLimitException` and `ServiceUnavailableException` tells the post-processor which exceptions to pass to the retry policy — 4xx validation errors and 404s are never retried.

### Configuring defaults in pyfly.yaml

Per-service overrides on `@service_client` take precedence, but you can set process-wide defaults in `pyfly.yaml` so new clients inherit sensible values without repeating them:

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

`ClientAutoConfiguration` reads these keys at startup and passes them as `default_retry` and `default_circuit_breaker` to `HttpClientBeanPostProcessor`. Any value set directly on `@service_client(circuit_breaker_failure_threshold=...)` overrides the default.

!!! tip "Set per-service timeouts low"
    The default `timeout: 30` is conservative. In production, each service should carry a `pyfly.yaml` override tuned to its SLA. A payments call that should complete in 500 ms should have `timeout: 2` — not 30 s — so a slow Payments instance fails fast and the circuit breaker can open before threads pile up.

---

## Auth, discovery, and deduplication

### Propagating identity downstream

When the Wallet service calls Payments, it often needs to carry the caller's identity — a JWT or an internal service token — so that Payments can enforce its own authorisation rules. The `headers` parameter on any stub method is treated specially by the post-processor: if the method declares a `headers: dict` parameter, the value is forwarded as HTTP request headers, not sent as a query string.

::: listing payments/client/payments_client_auth.py | Listing 11.6 — Forwarding auth headers per-call
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
        """POST /payments — body is the JSON payload; headers are forwarded."""
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

**How it works:** The post-processor checks whether a parameter named `headers` is present in the bound arguments and whether its value is a `dict`. If both conditions hold, the value is extracted from the query-parameter pool and forwarded as HTTP request headers. The handler passes the incoming `Authorization` header (or a freshly minted service token) as `headers={"Authorization": f"Bearer {token}"}`.

The `HttpxClientAdapter` also calls `inject_headers(headers)` on every request — this propagates the W3C `traceparent` and `tracestate` headers from the current observability context so distributed traces stitch across service boundaries without any application-level work.

!!! note "Service-to-service identity patterns"
    For internal services on a trusted network, a shared secret in an `X-Internal-Token` header is the simplest approach. For zero-trust architectures, consider mTLS (mutual TLS at the infrastructure layer) or a service mesh that injects identity certificates. For user-delegated calls, forward the original JWT. Whatever pattern you choose, the `headers` parameter gives you a clean injection point in the declarative client.

### Service discovery

When `base_url` is a static string like `http://payments-service:8080`, you are relying on DNS-based service discovery — a Kubernetes `Service` or a Consul record resolves `payments-service` to the correct cluster IP. This is the recommended starting point and sufficient for most deployments.

For environments that need dynamic URL resolution (multiple environments behind the same client class, feature-flagged routing), you can omit `base_url` from the decorator and supply it via configuration:

::: listing pyfly.yaml | Listing 11.7 — Per-environment base URL in pyfly.yaml
pyfly:
  client:
    timeout: 10

services:
  payments:
    base-url: "${PAYMENTS_SERVICE_URL:http://payments-service:8080}"
:::

A thin factory bean reads the config key and constructs the post-processor with a custom factory that injects the environment-resolved URL. This approach is covered in depth in the `HttpClientBeanPostProcessor` documentation; the important point is that the client class itself does not change — only the factory does.

### Request deduplication

Financial operations must be idempotent at the HTTP layer. If `create_payment` is called, times out, and is retried, Payments must not create two payment records. The conventional mechanism is an `Idempotency-Key` header: a stable, caller-chosen identifier (typically the command's UUID) that Payments uses to detect and deduplicate repeat requests.

::: listing wallet/application/handlers/settle_transfer_idempotent.py | Listing 11.8 — Idempotency-Key forwarded via headers parameter
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound

from payments.client.payments_client_auth import AuthenticatedPaymentsClient
from wallet.application.commands import SettleTransfer
from wallet.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class SettleTransferIdempotentHandler(
    CommandHandler[SettleTransfer, dict]
):
    """Debit wallet and submit payment with idempotency key."""

    def __init__(
        self,
        repo: WalletDomainRepository,
        payments: AuthenticatedPaymentsClient,
    ) -> None:
        self._repo = repo
        self._payments = payments

    async def do_handle(self, command: SettleTransfer) -> dict:
        wallet = await self._repo.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.debit(command.amount_cents, command.currency)
        await self._repo.save(wallet)

        idempotency_key = str(command.transfer_id)
        return await self._payments.create_payment(
            body={
                "wallet_id": command.wallet_id,
                "amount_cents": command.amount_cents,
                "currency": command.currency,
            },
            headers={"Idempotency-Key": idempotency_key},
        )
:::

**How it works:** `command.transfer_id` is the stable identifier for this business operation — it is determined before the command reaches the handler, so if the handler is called again for the same command (from a retry, a re-delivery, or a dead-letter replay), it passes the same `Idempotency-Key`. Payments stores the key alongside the created payment record and returns the existing record if the key has been seen before, rather than creating a second payment. This is a server-side concern; the client's job is simply to forward the key consistently.

---

## The experience tier: the BFF

### Why the frontend cannot talk to both services directly

When a mobile app or web frontend needs to display a wallet summary that includes pending payment instructions, it faces a choice: call Wallet for the balance, call Payments for the pending payments, and merge the results in the client — or talk to a single API that does the merging server-side. The first option creates two round trips, exposes both services' internal shapes to the client, and means the client must implement retry and error handling for two independent failure domains. The second option is the BFF pattern.

A Backend for Frontend is a lightweight service in the *experience tier* that composes responses from multiple domain services into a shape tailored to a specific frontend's needs. It is the place for response aggregation, field renaming to match the client's conventions, and caching of the composed result. It never talks to the database directly; it depends entirely on domain service clients.

### Building the Lumen BFF

The Wallet service also needs a typed client for the BFF to call it. It follows exactly the same pattern as `PaymentsClient`:

::: listing wallet/client/wallet_client.py | Listing 11.9 — WalletClient for the BFF tier
from __future__ import annotations

from pyfly.client import get, service_client


@service_client(
    base_url="http://wallet-service:8080",
    circuit_breaker=True,
    retry=3,
)
class WalletClient:
    """Typed HTTP client for the Wallet service."""

    @get("/wallets/{wallet_id}")
    async def get_wallet(self, wallet_id: str) -> dict:
        """GET /wallets/:wallet_id — fetch a wallet by ID."""
        ...

    @get("/wallets/{wallet_id}/balance")
    async def get_balance(self, wallet_id: str) -> dict:
        """GET /wallets/:wallet_id/balance — fetch current balance."""
        ...
:::

And the Payments service needs a `list_pending` endpoint for the BFF to query pending payment records by wallet:

::: listing payments/client/payments_client_bff.py | Listing 11.10 — Extended PaymentsClient for the BFF
from __future__ import annotations

from pyfly.client import get, post, service_client


@service_client(
    base_url="http://payments-service:8080",
    circuit_breaker=True,
    retry=3,
)
class PaymentsClient:
    """Typed HTTP client for the Payments service (BFF edition)."""

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

The BFF service composes the wallet balance from the Wallet service with the pending payments from the Payments service:

::: listing lumen_bff/application/bff_service.py | Listing 11.11 — BFF service composing Wallet + Payments
from __future__ import annotations

import asyncio

from pyfly.container import service

from payments.client.payments_client_bff import PaymentsClient
from wallet.client.wallet_client import WalletClient


@service
class WalletSummaryService:
    """Composes wallet balance and pending payments into a single view.

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

        balance_cents: int = 0
        if isinstance(wallet_data, dict):
            balance_cents = wallet_data.get("balance_cents", 0)

        pending_list: list = []
        if isinstance(pending, list):
            pending_list = pending

        return {
            "wallet_id": wallet_id,
            "balance_cents": balance_cents,
            "pending_payments": pending_list,
        }
:::

**How it works — the composition pattern:**

`asyncio.gather(...)` fires both upstream calls concurrently. The wallet call and the payments call run in parallel, so the composite latency is bounded by the slower of the two rather than their sum. This matters significantly at scale: if each service averages 50 ms, sequential calls cost 100 ms while concurrent calls cost 55 ms.

`return_exceptions=True` is critical for a BFF. Without it, a single failure in either service raises an exception and the caller receives nothing. With it, a failed coroutine returns the exception object as its result instead of propagating it. The service then inspects each result with `isinstance(wallet_data, dict)` and falls back gracefully — returning a partial response with a zero balance or an empty payment list rather than an HTTP 500. The caller can decide whether a partial response is acceptable; the BFF should make that decision explicit in its response shape (for instance, by including an `"errors"` key listing degraded fields).

The BFF's own `@service_client` wrappers on `WalletClient` and `PaymentsClient` handle retries and circuit breaking for each upstream call independently. If Payments is circuit-open, the wallet balance still appears; only the pending payments list is empty.

### The BFF controller

The BFF exposes its composed response through a standard PyFly web handler. The controller is thin — its only job is to delegate to the service:

::: listing lumen_bff/web/summary_controller.py | Listing 11.12 — BFF controller
from __future__ import annotations

from pyfly.container import rest_controller
from pyfly.web import get_mapping, request_mapping

from lumen_bff.application.bff_service import WalletSummaryService


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

**How it works:** The BFF controller imports no domain models and touches no repositories — it depends only on `WalletSummaryService`, which in turn depends only on typed client interfaces. The dependency chain is: controller → BFF service → declarative clients → remote HTTP. Each layer is independently testable: the controller with a mock service, the service with mock clients, the clients with a mock `HttpClientPort`.

!!! note "BFF scope and team ownership"
    A BFF is scoped to one frontend or one user journey — not one per microservice. Lumen might have a `lumen-mobile-bff` and a `lumen-web-bff`, each composing the same domain services but returning shapes optimised for their respective clients. The BFF is owned by the frontend team, not the domain team. Domain services expose stable contracts; BFFs adapt those contracts to client-specific shapes without coupling the domain services to any particular frontend's conventions.

!!! spring "Spring parity"
    The BFF pattern in PyFly mirrors the Spring Boot API Gateway / BFF approach where a thin Spring Boot application (often using Spring Cloud Gateway for routing) aggregates responses from multiple microservices. In the reactive Spring stack, `Mono.zip()` provides the same concurrent aggregation that `asyncio.gather()` does in Python. The `@FeignClient` in the BFF corresponds to `@service_client` in PyFly; the Spring `WebClient` approach of chaining `.flatMap()` calls corresponds to PyFly's `asyncio.gather()` + `isinstance` error handling. The team-ownership model — BFF owned by the frontend team, domain services owned by domain teams — is identical.

---

## What you built {.recap}

You started this chapter with a single Lumen service and ended it with an architecture that can scale in multiple dimensions. Here is what changed and why it matters.

**You extracted PaymentsService.** Payments now lives in its own process with its own deployment pipeline and its own data store. The Wallet service's handlers know nothing about how Payments stores payment records or which database engine it uses — all they know is the typed interface that `PaymentsClient` exposes.

**You declared the client, not the implementation.** `@service_client` with `@post`, `@get`, `@patch`, and `@delete` stubs gave you a typed interface that IDEs can navigate and type checkers can verify. `HttpClientBeanPostProcessor` generated the HTTP implementation at startup, drew the resilience configuration from `pyfly.yaml`, and registered the bean so it could be injected anywhere.

**You made the network resilient.** `circuit_breaker=True` and `retry=3` on the decorator wrapped every method with a shared circuit breaker and a retry policy — circuit breaker outside, retry inside — so a sustained Payments outage opens the circuit fast while transient errors recover automatically. Typed exceptions (`ServiceNotFoundException`, `ServiceUnavailableException`) give callers a clean signal about what went wrong without exposing raw HTTP status codes.

**You introduced the BFF tier.** `WalletSummaryService` composes two upstream calls with `asyncio.gather`, returns a partial response when one service is degraded, and exposes a single contract to the frontend. The BFF sits between the frontend and the domain services, absorbing their independent release cycles and shielding the frontend from their internal shapes.

The principles that carried you through this chapter also carry forward into Part IV:

- **Depend on the typed client, not on `httpx` directly.** The declaration is your contract; the implementation is a framework detail.
- **Resilience belongs in the client layer.** Configure it once on `@service_client`; every handler that uses the client inherits it.
- **BFFs compose; domain services provide.** Domain services own stable, fine-grained contracts; BFFs own the coarse-grained compositions that specific frontends need.

---

## Try it yourself {.exercises}

1. **Add a fourth endpoint and verify path interpolation.** Extend `PaymentsClient` with a `@get("/payments")` method that accepts `wallet_id: str` and `status: str = "pending"` as parameters. Call it from a test and assert that the generated HTTP request is `GET /payments?wallet_id=abc&status=pending`. Verify that changing the default to `status="completed"` and calling the method without a `status` argument sends `status=completed` in the query string.

2. **Test the BFF with degraded upstream services.** Write a unit test for `WalletSummaryService.get_summary` that mocks `WalletClient.get_wallet` to succeed and `PaymentsClient.list_pending` to raise `ServiceUnavailableException`. Assert that the method returns a dict with the correct `balance_cents` and an empty `pending_payments` list — confirming that the partial-response fallback works and a single upstream failure does not propagate as an exception to the BFF's caller.

3. **Tune circuit breaker thresholds for a brittle upstream.** Suppose Payments has a known flakiness window during its nightly batch run: it returns 503 on roughly 20% of requests for about 10 seconds before stabilising. Configure `PaymentsClient` with `circuit_breaker_failure_threshold=2` and `circuit_breaker_recovery_timeout=15.0` and write a test using a mock `HttpClientPort` that simulates two consecutive failures followed by success. Assert that the third call (the probe after the recovery timeout) succeeds and that the circuit transitions back to `CLOSED`.
