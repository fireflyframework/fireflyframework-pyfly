<span class="eyebrow">Chapter 17</span>

# Scheduling, Notifications, Webhooks & Callbacks {.chtitle}

::: figure art/openers/ch17.svg | &nbsp;

In Chapter 16 you gave Lumen a full test harness — unit tests for the
domain, integration tests for HTTP handlers, and container-managed
Testcontainers fixtures for the persistence layer. Lumen is now
well-tested and resilient. But it still lives entirely inside its own
process: it reacts to requests, but it never reaches out unprompted.

Real financial platforms are different. They send nightly account
statements, fire an SMS the moment funds land, receive payment-status
webhooks from Stripe at midnight, and call back partner systems to
confirm that a disbursement was booked. That is four distinct
integration patterns — scheduling, notifications, inbound webhooks, and
outbound callbacks — and this chapter covers all of them.

By the end of the chapter Lumen will:

- run a **nightly scheduled job** that tallies daily transaction totals
  using `@scheduled` with a cron expression;
- send a **"funds received" email and push notification** via PyFly's
  pluggable `EmailService` and `PushService` ports;
- accept **inbound webhooks** from a payment provider, verifying the
  HMAC-SHA256 signature, deduplicating replays, and dispatching to a
  typed listener;
- dispatch **outbound callbacks** to partner systems, signing each
  payload, retrying on transient failures, and recording every
  delivery attempt.

Install the two optional extras before you start:

```
uv add "pyfly[scheduling,notifications]"
```

---

## Scheduled tasks

### Why schedule instead of trigger?

Many operations in a financial platform should not wait for an HTTP
request to arrive. Nightly reconciliation must run at 02:00 regardless
of whether any user is active. A cache-warming pass should fire 30
seconds after startup — before real traffic arrives — not when the
first slow request triggers a miss. A heartbeat metric should be
emitted every 10 seconds so that the ops dashboard shows a live
signal, not a stale reading.

PyFly's scheduling module provides a declarative, decorator-driven way
to define all three patterns without manually managing threads, event
loops, or timer wheels.

::: figure art/figures/17-integrations.svg | Figure 17.1 — The four\
integration layers added in this chapter. Scheduled tasks fire\
internally; notifications flow outward to users; inbound webhooks\
arrive from partners; outbound callbacks close the feedback loop.

### The @scheduled decorator

`@scheduled` marks any `async` method on a `@service` bean for periodic
execution. It accepts exactly one *trigger*: `fixed_rate`, `fixed_delay`,
or `cron`. Providing zero or more than one trigger raises a `ValueError`
at decoration time, so mistakes surface at startup, not silently at
three in the morning.

::: listing lumen/ledger/daily_rollup.py | Listing 17.1 — Nightly statement rollup with @scheduled
from datetime import timedelta

from pyfly.container import service
from pyfly.scheduling import scheduled


@service
class DailyRollupService:
    """Aggregates transaction totals once per night at 02:00 UTC."""

    def __init__(self, tx_repo, statement_repo) -> None:
        self._tx = tx_repo
        self._statements = statement_repo

    @scheduled(cron="0 2 * * *")
    async def run(self) -> None:
        totals = await self._tx.aggregate_yesterday()
        await self._statements.save_daily(totals)
:::

`@scheduled(cron="0 2 * * *")` fires every day at 02:00 UTC. The
scheduler calculates `seconds_until_next()` via `CronExpression`, sleeps
exactly that long, then submits the method to the executor.

That is all the wiring Lumen needs. With `pyfly[scheduling]` installed,
`SchedulingAutoConfiguration` automatically:

1. registers a `TaskScheduler` bean;
2. scans every `@service` bean for `@scheduled` methods;
3. starts the scheduler during `ApplicationContext` startup;
4. stops it gracefully on shutdown.

No `SchedulerManager` required.

### fixed_rate vs. fixed_delay

`fixed_rate` measures from the **start** of one execution to the start
of the next. `fixed_delay` measures from the **end** of one execution
to the start of the next. Use `fixed_rate` for heartbeats and metrics
where you want a steady cadence regardless of execution time. Use
`fixed_delay` when you need a guaranteed breathing gap — for example,
when polling an upstream API that rate-limits on request frequency.

::: listing lumen/health/monitor.py | Listing 17.2 — fixed_rate heartbeat and fixed_delay poll
from datetime import timedelta

from pyfly.container import service
from pyfly.scheduling import scheduled


@service
class HealthMonitor:

    @scheduled(
        fixed_rate=timedelta(seconds=10),
        initial_delay=timedelta(seconds=5),
    )
    async def heartbeat(self) -> None:
        """Emit a liveness metric every 10 s, starting 5 s after startup."""
        # metrics.gauge("lumen.up", 1)
        pass


@service
class ExchangeRatePoller:

    def __init__(self, fx_repo) -> None:
        self._repo = fx_repo

    @scheduled(fixed_delay=timedelta(minutes=5))
    async def poll(self) -> None:
        """Fetch the latest exchange rates, then wait 5 min before repeating."""
        rates = await self._repo.fetch_latest()
        await self._repo.store(rates)
:::

`initial_delay` postpones the very first run; it is available for both
`fixed_rate` and `fixed_delay` (ignored for `cron` triggers, which
always wait for the next matching calendar instant).

### CronExpression

You can also use `CronExpression` directly — useful when you need to
display upcoming schedule times in a UI, or to validate a user-supplied
expression before storing it.

::: listing lumen/ledger/schedule_preview.py | Listing 17.3 — Using CronExpression standalone
from pyfly.scheduling import CronExpression


def preview_rollup_schedule(expression: str, n: int = 5) -> list[str]:
    """Return the next N fire times for a given cron expression."""
    cron = CronExpression(expression)
    return [str(t) for t in cron.next_n_fire_times(n)]
:::

`CronExpression` accepts both the standard 5-field format
(`min hour dom month dow`) and the Spring-style 6-field format with
seconds first (`sec min hour dom month dow`). The Spring `?` wildcard is
also normalised to `*` transparently.

| Expression | Fires |
|---|---|
| `* * * * *` | Every minute |
| `0 * * * *` | Every hour, on the hour |
| `0 2 * * *` | Every day at 02:00 |
| `0 9 * * 1-5` | Weekdays at 09:00 |
| `30 2 1 * *` | 1st of each month at 02:30 |
| `*/15 * * * *` | Every 15 minutes |
| `0 0 12 * * *` | Noon every day (6-field, seconds-first) |

### Time-zone-aware cron

By default cron expressions are evaluated in **UTC**. Pass `zone` with
an IANA time-zone name to evaluate fire times in that zone instead:

```python
@scheduled(cron="0 2 * * *", zone="America/New_York")
async def close_books(self) -> None:
    """02:00 New York time — follows DST automatically."""
    ...
```

The same `zone` parameter is available on `CronExpression`:

```python
cron = CronExpression("0 9 * * *", zone="Europe/Madrid")
next_run = cron.next_fire_time()  # zone-aware datetime
```

DST transitions are handled by the `zoneinfo` database — PyFly does not
require any manual offset adjustment.

### Distributed locking

When you run multiple instances of Lumen behind a load balancer, every
instance schedules the same `@scheduled` methods. Without coordination,
the nightly rollup would fire once per instance and write duplicate
statements. The `lock` parameter solves this the same way Spring's
`@SchedulerLock` (ShedLock) does: before each tick the scheduler tries
to acquire a named lock, and **skips the run** if the lock is already
held elsewhere.

```python
@scheduled(cron="0 2 * * *", lock=True, lock_ttl=timedelta(minutes=5))
async def run(self) -> None:
    """lock=True auto-names the lock 'DailyRollupService.run'."""
    ...
```

- `lock=True` — derives the lock name from `"ClassName.method_name"`.
- `lock="shared-name"` — explicit name; useful when two methods must
  be mutually exclusive.
- `lock_ttl` — safety-valve TTL; set it comfortably longer than the
  job's worst-case runtime.

Out of the box `TaskScheduler` uses `LocalLock`, which always acquires —
single-instance behaviour is unchanged. For cross-process coordination,
implement `DistributedLock` and register it as a bean:

::: listing lumen/infra/redis_lock.py | Listing 17.4 — Redis-backed DistributedLock
from pyfly.container import bean, configuration
from pyfly.scheduling import DistributedLock


class RedisLock:
    """Best-effort named lock backed by Redis SET NX PX."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def try_acquire(self, name: str, ttl: float) -> bool:
        ok = await self._redis.set(
            f"pyfly:lock:{name}", "1",
            nx=True, px=int(ttl * 1000),
        )
        return ok is True

    async def release(self, name: str) -> None:
        await self._redis.delete(f"pyfly:lock:{name}")


@configuration
class LockConfig:

    @bean
    def distributed_lock(self) -> DistributedLock:
        import redis.asyncio as aioredis
        client = aioredis.from_url("redis://localhost:6379/1")
        return RedisLock(client)
:::

`SchedulingAutoConfiguration` automatically detects the `DistributedLock`
bean in the container and passes it to the `TaskScheduler`. Any object
with conforming `try_acquire` and `release` coroutines satisfies the
protocol.

### @async_method

`@async_method` marks a method for fire-and-forget execution via the
`TaskExecutorPort`. The caller returns immediately; the framework routes
the call through the configured executor in the background:

```python
from pyfly.scheduling import async_method


@service
class AlertService:

    @async_method
    async def send_alert(self, msg: str) -> None:
        """Caller does not await — AlertService dispatches asynchronously."""
        ...
```

Under the hood `@async_method` sets `__pyfly_async__ = True` on the
function; the framework picks this up and submits the coroutine to the
`TaskExecutorPort`.

!!! spring "Spring parity"
    `@scheduled(fixed_rate=...)` mirrors Spring's
    `@Scheduled(fixedRate=...)`. `@scheduled(fixed_delay=...)` mirrors
    `@Scheduled(fixedDelay=...)`. `@scheduled(cron=...)` mirrors
    `@Scheduled(cron=...)`. `zone=` mirrors Spring's `zone` attribute.
    `lock=True` mirrors ShedLock's `@SchedulerLock`. `@async_method`
    mirrors Spring's `@Async`.

### Configuration reference

```yaml
pyfly:
  scheduling:
    enabled: true          # set false to disable all loops
    thread-pool:
      max-workers: 4       # threads for ThreadPoolTaskExecutor
```

When `enabled` is `false`, `TaskScheduler` will not start any loops and
all `@scheduled` methods are silently ignored.

---

## Notifications

Lumen needs to tell customers that their money has arrived. That means
email for the balance notification, and optionally an SMS or a mobile
push for the real-time "funds received" alert.

PyFly's notifications module defines three **port protocols** and three
**default services**. Your business logic depends on the protocols; the
concrete provider adapters — SMTP, SendGrid, Twilio, Firebase — live
behind the port boundary and can be swapped without touching a single
line of domain code.

### The port hierarchy

| Protocol | Service class | Method |
|---|---|---|
| `EmailProvider` | `DefaultEmailService` | `send(EmailMessage) -> NotificationResult` |
| `SmsProvider` | `DefaultSmsService` | `send(SmsMessage) -> NotificationResult` |
| `PushProvider` | `DefaultPushService` | `send(PushMessage) -> NotificationResult` |

`DefaultEmailService`, `DefaultSmsService`, and `DefaultPushService` are
thin wrappers: they delegate to a provider and catch any provider
exception, returning a structured `NotificationResult` with
`status=FAILED` and the error string rather than propagating the
exception. This means a transient SendGrid outage does not take down the
deposit handler.

### Messages and results

::: listing lumen/notifications/models_overview.py | Listing 17.5 — The core DTOs
from pyfly.notifications import (
    EmailMessage,
    NotificationResult,
    PushMessage,
    SmsMessage,
)

# Email — full field set
email = EmailMessage(
    to=["alice@example.com"],
    sender="no-reply@lumenbank.com",
    subject="Funds received",
    body_text="EUR 250.00 has been credited to your wallet.",
    body_html=(
        "<p><strong>EUR 250.00</strong> has been credited "
        "to your wallet.</p>"
    ),
)

# SMS — compact
sms = SmsMessage(
    to="+34600000001",
    body="Lumen: EUR 250.00 received. New balance: EUR 750.00.",
    sender="LUMEN",
)

# Push — structured payload
push = PushMessage(
    device_tokens=["FCM_TOKEN_GOES_HERE"],
    title="Funds received",
    body="EUR 250.00 credited",
    data={"wallet_id": "w-001", "amount": "250.00"},
)
:::

`NotificationResult` carries `id`, `provider`, `status`
(`EmailStatus.SENT | DELIVERED | FAILED | ...`), an optional
`provider_id` (e.g. the SendGrid message ID), and an optional `error`.

### Wiring the SMTP provider

For development and self-hosted deployments, `SmtpEmailProvider` uses
Python's stdlib `smtplib` from a thread pool, so the async event loop
is never blocked:

::: listing lumen/notifications/config.py | Listing 17.6 — SMTP provider wired as a @bean
from pyfly.container import bean, configuration
from pyfly.notifications import DefaultEmailService, EmailService
from pyfly.notifications.providers.smtp import SmtpEmailProvider


@configuration
class NotificationConfig:

    @bean
    def email_provider(self) -> SmtpEmailProvider:
        return SmtpEmailProvider(
            "smtp.lumenbank.internal",
            port=587,
            username="notifications",
            password="s3cr3t",
            use_tls=True,
        )

    @bean
    def email_service(
        self, provider: SmtpEmailProvider,
    ) -> EmailService:
        return DefaultEmailService(provider=provider)
:::

`SmtpEmailProvider` accepts `host`, `port` (default `587`), `username`,
`password`, and `use_tls` (default `True`). Swap the provider for
`SendGridEmailProvider` or `ResendEmailProvider` by changing a single
`@bean` method — `DefaultEmailService` does not care which provider
sits behind it.

!!! tip "Available providers"
    `pyfly.notifications` ships eight built-in adapters:
    `DummyEmailProvider` / `DummySmsProvider` / `DummyPushProvider`
    (log-only, for dev/tests), `SmtpEmailProvider`,
    `SendGridEmailProvider`, `ResendEmailProvider` (email),
    `TwilioSmsProvider` (SMS), and `FirebasePushProvider` (push). All
    satisfy their respective `*Provider` protocol.

### Sending a "funds received" notification

Now wire `EmailService` and `PushService` into the deposit handler:

::: listing lumen/wallet/deposit_handler.py | Listing 17.7 — Notifying on deposit
from pyfly.container import service
from pyfly.notifications import (
    DefaultPushService,
    EmailMessage,
    EmailService,
    PushMessage,
    PushService,
)


@service
class DepositNotificationService:
    """Sends email + push notifications when a deposit is confirmed."""

    def __init__(
        self,
        email_service: EmailService,
        push_service: PushService,
    ) -> None:
        self._email = email_service
        self._push = push_service

    async def notify(
        self,
        wallet_id: str,
        amount: str,
        email: str,
        device_token: str,
    ) -> None:
        await self._email.send(EmailMessage(
            to=[email],
            sender="no-reply@lumenbank.com",
            subject=f"Funds received: {amount}",
            body_text=(
                f"EUR {amount} has been credited to wallet "
                f"{wallet_id}."
            ),
        ))
        await self._push.send(PushMessage(
            device_tokens=[device_token],
            title="Funds received",
            body=f"EUR {amount} credited",
            data={"wallet_id": wallet_id, "amount": amount},
        ))
:::

Both calls return a `NotificationResult`; you can inspect the `status`
field if you want to log failures or schedule retries.

!!! spring "Spring parity"
    `EmailService` / `SmsService` / `PushService` are the Python
    equivalents of Spring's `JavaMailSender` (email) and third-party
    Spring integrations for SMS and push. The hexagonal port/adapter
    split is identical: your domain code depends on the protocol;
    concrete provider adapters live in the infrastructure layer.

---

## Inbound webhooks

Stripe will POST a `payment_intent.succeeded` event to Lumen whenever a
customer tops up their wallet from a card. Lumen must:

1. **verify the HMAC-SHA256 signature** to reject forged payloads;
2. **deduplicate** replays using the idempotency key so a retry does
   not credit a wallet twice;
3. **dispatch** the verified event to a typed listener.

PyFly's `pyfly.webhooks` module handles all three steps.

### WebhookEvent and AbstractWebhookEventListener

Every inbound event is modelled as a `WebhookEvent` dataclass:

```python
@dataclass
class WebhookEvent:
    id: str               # auto-generated UUID
    source: str           # e.g. "stripe"
    event_type: str       # from body["type"]
    headers: dict[str, str]
    body: dict[str, Any]
    raw_body: bytes
    received_at: datetime
    idempotency_key: str | None
```

Subclass `AbstractWebhookEventListener` and set `source` to the name
you will pass to `WebhookProcessor.process()`:

::: listing lumen/webhooks/stripe_listener.py | Listing 17.8 — Stripe webhook listener
from pyfly.container import service
from pyfly.webhooks import AbstractWebhookEventListener, WebhookEvent


@service
class StripeWebhookListener(AbstractWebhookEventListener):
    source = "stripe"

    def __init__(self, deposit_svc) -> None:
        self._deposits = deposit_svc

    async def handle(self, event: WebhookEvent) -> None:
        if event.event_type == "payment_intent.succeeded":
            pi = event.body.get("data", {}).get("object", {})
            wallet_id = pi.get("metadata", {}).get("wallet_id")
            amount_cents = pi.get("amount_received", 0)
            if wallet_id:
                await self._deposits.credit(
                    wallet_id=wallet_id,
                    cents=amount_cents,
                )

    async def on_error(
        self, event: WebhookEvent, error: BaseException,
    ) -> None:
        # Override to DLQ or page on-call.
        pass
:::

`on_error` is called when `handle` raises; the default is a no-op. You
can override it to publish to a dead-letter queue or emit a metric.

### WebhookProcessor — verify, dedupe, dispatch

`WebhookProcessor` wires together a signature validator, an idempotency
store, and a list of listeners:

::: listing lumen/webhooks/processor_config.py | Listing 17.9 — Assembling WebhookProcessor
from pyfly.container import bean, configuration
from pyfly.webhooks import (
    HmacSignatureValidator,
    WebhookProcessor,
)
from lumen.webhooks.stripe_listener import StripeWebhookListener


@configuration
class WebhookConfig:

    @bean
    def webhook_processor(
        self, stripe_listener: StripeWebhookListener,
    ) -> WebhookProcessor:
        return WebhookProcessor(
            listeners=[stripe_listener],
            signature_validators={
                "stripe": HmacSignatureValidator(
                    secret="whsec_REPLACE_ME",
                ),
            },
        )
:::

`HmacSignatureValidator` expects the Stripe-style `sha256=<hex>` header
format and uses `hmac.compare_digest` for a constant-time comparison.
The `header_prefix` parameter can be changed if your provider uses a
different prefix.

### Handling a webhook in an HTTP handler

Call `processor.process()` from your inbound HTTP handler. Pass the raw
request body (unmodified bytes) for signature verification:

::: listing lumen/webhooks/stripe_handler.py | Listing 17.10 — Inbound Stripe webhook endpoint
from pyfly.container import service
from pyfly.web import Request, Response, router
from pyfly.webhooks import WebhookProcessor


@service
class StripeWebhookHandler:

    def __init__(self, processor: WebhookProcessor) -> None:
        self._processor = processor

    @router.post("/webhooks/stripe")
    async def receive(self, request: Request) -> Response:
        raw_body = await request.body()
        headers = {
            "X-Signature": request.headers.get(
                "Stripe-Signature", ""
            ),
            "X-Idempotency-Key": request.headers.get(
                "Stripe-Idempotency-Key",
                request.headers.get("Idempotency-Key", ""),
            ),
        }
        try:
            await self._processor.process(
                source="stripe",
                raw_body=raw_body,
                headers=headers,
            )
        except ValueError:
            return Response(status=400, body=b"invalid signature")
        return Response(status=200, body=b"ok")
:::

The `process()` signature accepts `signature_header` and
`idempotency_header` keyword arguments to override the default header
names (`X-Signature` and `X-Idempotency-Key`).

**What happens inside `process()`:**

1. The validator is looked up by `source`; if none is registered a
   `NoOpSignatureValidator` is used (always passes — safe for dev but
   not production).
2. If signature validation fails, `ValueError` is raised immediately
   and no listeners are called.
3. The raw body is decoded as JSON; on failure the raw bytes are stored
   under `body["_raw"]`.
4. If `idempotency_key` is present and has already been seen, the
   event is returned but listeners are **not** called.
5. Each listener for the source is called in registration order; if
   one raises, the error is logged and `on_error` is called before
   continuing to the next listener.

!!! note "In-memory idempotency store"
    The default `InMemoryWebhookEventStore` holds seen keys in a Python
    `set`. For production clusters, implement the `WebhookEventStore`
    protocol backed by Redis or a database:
    ```python
    class RedisEventStore:
        async def already_processed(
            self, key: str,
        ) -> bool: ...
        async def remember(self, key: str) -> None: ...
    ```
    Pass it as `event_store=RedisEventStore(...)` to `WebhookProcessor`.

---

## Outbound callbacks

When Lumen books a disbursement to a partner bank, that partner expects
a `DisbursementSettled` POST to their webhook URL — signed, retried on
failure, and auditable. PyFly's `pyfly.callbacks` module handles the
outbound side.

### Subscriptions and config

Each partner is modelled as a `CallbackConfig` — a tenant-scoped record
that holds the webhook secret, the list of event subscriptions, and
retry policy:

::: listing lumen/callbacks/register_partner.py | Listing 17.11 — Registering a partner callback
from pyfly.callbacks import (
    CallbackConfig,
    CallbackSubscription,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


async def register_clearance_bank(configs) -> None:
    await configs.save(CallbackConfig(
        tenant_id="lumen",
        name="clearance-bank",
        secret="cb-secret-xyz",
        max_attempts=5,
        backoff_ms=2_000,
        subscriptions=[
            CallbackSubscription(
                event_type="DisbursementSettled",
                target_url=(
                    "https://api.clearancebank.example.com"
                    "/hooks/lumen"
                ),
            ),
            CallbackSubscription(
                event_type="*",
                target_url=(
                    "https://audit.clearancebank.example.com"
                    "/all-events"
                ),
            ),
        ],
    ))
:::

`event_type="*"` is a catch-all: every event dispatched for this
tenant matches. Named types match only the exact event type string.

### Dispatching an event

`CallbackDispatcher.dispatch()` fans the event out to every matching
subscription:

::: listing lumen/callbacks/dispatcher_config.py | Listing 17.12 — Wiring and calling CallbackDispatcher
from pyfly.callbacks import (
    CallbackDispatcher,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)
from pyfly.container import bean, configuration


@configuration
class CallbackConfig_:

    @bean
    def callback_configs(self) -> InMemoryCallbackConfigRepository:
        return InMemoryCallbackConfigRepository()

    @bean
    def callback_executions(
        self,
    ) -> InMemoryCallbackExecutionRepository:
        return InMemoryCallbackExecutionRepository()

    @bean
    def callback_dispatcher(
        self,
        configs: InMemoryCallbackConfigRepository,
        executions: InMemoryCallbackExecutionRepository,
    ) -> CallbackDispatcher:
        return CallbackDispatcher(
            configs=configs,
            executions=executions,
        )
:::

Then in the domain service:

```python
results = await dispatcher.dispatch(
    "lumen",            # tenant_id
    "DisbursementSettled",
    {"id": "txn-009", "amount": 500_00, "currency": "EUR"},
)
```

`dispatch()` returns a list of `CallbackExecution` records — one per
matching subscription — each with `status`, `attempts`,
`response_status`, and `delivered_at`.

### HMAC signing and retry logic

When `CallbackConfig.secret` is set, `CallbackDispatcher` signs the
canonical JSON payload before every POST:

```python
# canonical body — compact, keys sorted
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
sig = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
headers["X-Pyfly-Signature"] = f"sha256={sig}"
```

The recipient can verify the signature using PyFly's own
`HmacSignatureValidator` (the same class used for inbound webhooks).

**Retry policy:**

- The dispatcher retries up to `max_attempts` times (default `5`).
- Between retries it applies exponential backoff:
  `delay = min(backoff_ms * 2^(attempt-1), 300_000) ms`.
- Only *transient* HTTP status codes trigger a retry:
  `408`, `429`, `500`, `502`, `503`, `504`, or any `>= 500`.
- Permanent client errors (`4xx` except `408`/`429`) mark the
  execution as `FAILED` immediately without retrying.
- On success (`2xx`) the execution is marked `DELIVERED` and
  `delivered_at` is stamped.

::: listing lumen/callbacks/models_overview.py | Listing 17.13 — CallbackExecution status lifecycle
from pyfly.callbacks import CallbackStatus

# After a successful delivery:
assert execution.status == CallbackStatus.DELIVERED
assert execution.delivered_at is not None
assert execution.response_status == 200

# After all retries are exhausted:
assert execution.status == CallbackStatus.FAILED
assert execution.attempts == 5
assert execution.last_error is not None
:::

### SSRF protection — authorized domains

The `authorized_domains` field on `CallbackConfig` acts as an allowlist.
When set, `CallbackDispatcher` checks that the target URL's hostname
matches one of the allowed domains before making any HTTP request. A
URL that fails the check is immediately marked `FAILED` with
`last_error="Domain not authorized"` — no outbound request is made.

```python
from pyfly.callbacks import AuthorizedDomain, CallbackConfig

config = CallbackConfig(
    tenant_id="lumen",
    name="safe-config",
    secret="s3cr3t",
    authorized_domains=[
        AuthorizedDomain(domain="clearancebank.example.com"),
    ],
    subscriptions=[...],
)
```

Subdomains of allowed domains are also accepted (e.g. `api.clearancebank.example.com`).

!!! spring "Spring parity"
    PyFly's `@scheduled` / `CronExpression` / `TaskScheduler` trio
    mirrors Spring's `@Scheduled` / `CronExpression` /
    `ThreadPoolTaskScheduler`. Notification ports map to Spring's
    `MailSender` / `JavaMailSender`. `WebhookProcessor` corresponds to
    a Spring `@RestController` + Spring Security's HMAC
    `HmacRequestMatcher`. `CallbackDispatcher` with HMAC signing and
    retry mirrors Spring's `WebhookPublisher` pattern from Spring
    Modulith.

---

## What you built {.recap}

You extended Lumen into a connected system that operates independently of
incoming requests:

- **Scheduled tasks** — `@scheduled` with `cron`, `fixed_rate`, and
  `fixed_delay` triggers run work on a calendar or timer. `CronExpression`
  drives fire-time calculations, including time-zone-aware scheduling.
  `lock=True` serialises cluster-wide execution via the `DistributedLock`
  port. `@async_method` offloads fire-and-forget work to the executor.

- **Notifications** — `EmailService`, `SmsService`, and `PushService`
  port protocols decouple business logic from provider adapters (SMTP,
  SendGrid, Resend, Twilio, Firebase). `DefaultEmailService` and its
  siblings catch provider errors and return structured `NotificationResult`
  values rather than propagating exceptions.

- **Inbound webhooks** — `AbstractWebhookEventListener` defines typed
  consumers. `WebhookProcessor` gates every event with `HmacSignatureValidator`
  and `WebhookEventStore` before dispatching to listeners.
  `on_error()` hooks allow DLQ integration without breaking the
  dispatch loop.

- **Outbound callbacks** — `CallbackDispatcher` fans events out to
  `CallbackSubscription` targets, signs payloads with HMAC-SHA256 under
  the `X-Pyfly-Signature` header, and retries on transient failures
  with exponential backoff. `CallbackExecution` records provide a full
  delivery audit trail. `authorized_domains` prevents SSRF.

---

## Try it yourself {.exercises}

1. **Cluster lock.** Add a second instance of `DailyRollupService` to a
   test that spins up two `ApplicationContext` instances sharing a
   `FakeDistributedLock` (one that only returns `True` for the first
   acquirer). Assert that `run()` is called exactly once across both
   instances for a single cron tick.

2. **Provider swap.** Replace `SmtpEmailProvider` with a
   `DummyEmailProvider` in the test suite. Write a test that deposits
   EUR 100 into wallet `w-001` via the deposit handler and asserts that
   `DummyEmailProvider.last_message` contains the wallet ID and amount
   in its `body_text`.

3. **Signature replay attack.** Write a test that calls
   `StripeWebhookHandler.receive()` twice with the same raw body,
   headers, and idempotency key. Assert that the first call returns 200
   and credits the wallet, and the second call also returns 200 (the
   duplicate is silently ignored by `InMemoryWebhookEventStore`) but
   does **not** credit the wallet a second time.
