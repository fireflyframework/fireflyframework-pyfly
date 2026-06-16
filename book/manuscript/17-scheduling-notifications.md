<span class="eyebrow">Chapter 17</span>

# Scheduling, Notifications, Webhooks & Callbacks {.chtitle}

::: figure art/openers/ch17.svg | &nbsp;

In Chapter 16 you gave Lumen a full test harness — unit tests for the domain, CQRS flow tests through the real bus, and a SQLite adapter test that proves true persistence. Lumen is now well-tested and resilient. But it still lives entirely inside its own process: it reacts to requests, but it never reaches out unprompted.

Real financial platforms are different. They send nightly account statements, fire an SMS the moment funds land, receive payment-status webhooks from a payment provider at midnight, and call back partner systems to confirm that a disbursement was booked. That is four distinct integration patterns — scheduling, notifications, inbound webhooks, and outbound callbacks — and this chapter covers all of them.

By the end of the chapter Lumen will:

- run a **nightly scheduled job** that tallies daily wallet balances using `@scheduled` with a cron expression;
- send a **"funds received" email and push notification** via PyFly's pluggable `EmailService` and `PushService` ports, triggered by the real `FundsDeposited` domain event;
- accept **inbound webhooks** from an illustrative payment provider, verifying the HMAC-SHA256 signature, deduplicating replays, and dispatching to a typed listener;
- dispatch **outbound callbacks** to partner systems, signing each payload, retrying on transient failures, and recording every delivery attempt.

Install the two optional extras before you start:

```
uv add "pyfly[scheduling,notifications]"
```

!!! note "New term: optional extras"
    An *extra* is an opt-in slice of a package's dependencies. `pyfly` keeps
    its core lean and ships heavier capabilities — scheduling, notifications,
    and the rest — behind named extras so you only install what you use.
    `pyfly[scheduling,notifications]` pulls in both. The square-bracket syntax
    is standard Python packaging; `uv add` records it in your `pyproject.toml`
    so the next `uv sync` reinstalls them. If you later see `ModuleNotFoundError`
    for `croniter` or a notifications provider, you skipped this line — re-run it.

This chapter targets PyFly **v26.6.110**. Every code listing below matches the
real Lumen source under `samples/lumen/src/lumen`, and every framework API was
checked against `pyfly` itself, so what you build here runs unchanged.

---

## Scheduled tasks

### Why schedule instead of trigger?

Many operations in a financial platform cannot wait for an HTTP request to arrive. Nightly reconciliation must run at 02:00 regardless of whether any user is active. A cache-warming pass should fire 30 seconds after startup — before real traffic arrives — not when the first slow request triggers a miss. A heartbeat metric should emit every 10 seconds so the ops dashboard shows a live signal, not a stale reading.

PyFly's scheduling module provides a declarative, decorator-driven way to define all three patterns without manually managing threads, event loops, or timer wheels.

::: figure art/figures/17-integrations.svg | Figure 17.1 — The four\
integration layers added in this chapter. Scheduled tasks fire\
internally; notifications flow outward to users; inbound webhooks\
arrive from partners; outbound callbacks close the feedback loop.

### The @scheduled decorator

**`@scheduled`** marks any `async` method on a `@service` bean for periodic execution. It accepts exactly one *trigger*: `fixed_rate`, `fixed_delay`, or `cron`. Providing zero or more than one trigger raises a `ValueError` at decoration time, so mistakes surface at startup rather than silently at three in the morning.

!!! note "New term: trigger"
    A *trigger* is the rule that decides *when* a scheduled method runs. You
    pick exactly one: `cron` (a calendar expression like "every day at 02:00"),
    `fixed_rate` (a steady interval such as "every 10 seconds"), or `fixed_delay`
    (a gap measured after each run finishes). One method, one trigger.

Let us build the nightly rollup one decision at a time.

**Step 1 — Create the service file.** In the Lumen tree, add `daily_rollup.py`
under a `ledger` package. The class is an ordinary `@service` — a plain Python
class that PyFly registers in its dependency-injection container and constructs
for you. Because it is a managed bean, you can ask for the `WalletRepository` in
the constructor and the framework hands it over; you never call `new` yourself.

**Step 2 — Pick the trigger.** This job must run once a night, on the clock, so
the trigger is `cron`. The expression `"0 2 * * *"` reads field-by-field as
*minute 0, hour 2, every day-of-month, every month, every day-of-week* — i.e.
02:00 every day.

**Step 3 — Write the work.** Inside the method, load every wallet, sum the
persisted `balance_minor` integers, and (for now) log the total. In production
you would write the snapshot to a reporting table or ship it downstream; logging
keeps the example focused on the *scheduling*, not the bookkeeping.

::: listing lumen/ledger/daily_rollup.py | Listing 17.1 — Nightly wallet balance rollup with @scheduled
from datetime import timedelta

from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.scheduling import scheduled


@service
class DailyRollupService:
    """Tallies all wallet balances once per night at 02:00 UTC."""

    def __init__(self, wallet_repo: WalletRepository) -> None:
        self._wallets = wallet_repo

    @scheduled(cron="0 2 * * *")
    async def run(self) -> None:
        wallets = await self._wallets.find_all()
        # find_all() returns WalletEntity rows; balance_minor is the
        # persisted integer (cents).
        total_minor_units = sum(w.balance_minor for w in wallets)
        # Persist or ship the nightly snapshot; here we log it.
        print(
            f"[rollup] {len(wallets)} wallets, "
            f"total {total_minor_units / 100:.2f} "
            f"(minor units: {total_minor_units})"
        )
:::

**How it works.** `@scheduled(cron="0 2 * * *")` fires every day at 02:00 UTC. The scheduler calculates `seconds_until_next()` via `CronExpression`, sleeps exactly that long, then submits the method to the executor.

That is all the wiring Lumen needs. With `pyfly[scheduling]` installed, `SchedulingAutoConfiguration` automatically:

1. registers a `TaskScheduler` bean;
2. scans every `@service` bean for `@scheduled` methods;
3. starts the scheduler during `ApplicationContext` startup;
4. stops it gracefully on shutdown.

No `SchedulerManager` required.

!!! note "New term: auto-configuration"
    *Auto-configuration* is PyFly noticing what is on your classpath and wiring
    the matching machinery for you. `SchedulingAutoConfiguration` only activates
    when `croniter` (pulled in by the `scheduling` extra) is importable — so the
    scheduler appears the moment you install the extra and stays absent
    otherwise. This is the same "convention over configuration" idea Spring Boot
    made famous; you can always override a bean by declaring your own.

**Run it.** Waiting until 02:00 to see your first tick is no fun, so temporarily
change the trigger to fire every minute — `@scheduled(cron="* * * * *")` — and
start the app from the `samples/lumen` directory:

```bash
uv run pyfly run --server uvicorn
```

At the top of the next minute you should see your rollup line in the logs (an
empty database simply reports zero wallets):

```text
[rollup] 0 wallets, total 0.00 (minor units: 0)
```

Open a wallet and deposit into it (see the curl recipes in Chapter 7), wait for
the next minute, and the totals move:

```text
[rollup] 1 wallets, total 15.00 (minor units: 1500)
```

Stop the app with Ctrl-C and **change the trigger back to `"0 2 * * *"`** before
committing — the every-minute cron was only a probe.

!!! note "What just happened"
    You did not start a thread, open an event loop, or register a timer. You
    wrote one `@service` with one `@scheduled` method, and the framework
    discovered it at startup, computed the next fire time from the cron
    expression, slept until then, and ran your coroutine — repeating forever.
    The scheduler is *declarative*: you state *when*, PyFly handles *how*.

### fixed_rate vs. fixed_delay

**`fixed_rate`** measures from the **start** of one execution to the start of the next. **`fixed_delay`** measures from the **end** of one execution to the start of the next. Use `fixed_rate` for heartbeats and metrics where you need a steady cadence regardless of execution time. Use `fixed_delay` when you need a guaranteed breathing gap — for example, when polling an upstream API that rate-limits on request frequency.

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

`initial_delay` postpones the first run; it is available for both `fixed_rate` and `fixed_delay` (ignored for `cron` triggers, which always wait for the next matching calendar instant).

### CronExpression

**`CronExpression`** is also usable directly — convenient when you need to display upcoming schedule times in a UI or validate a user-supplied expression before storing it.

::: listing lumen/ledger/schedule_preview.py | Listing 17.3 — Using CronExpression standalone
from pyfly.scheduling import CronExpression


def preview_rollup_schedule(expression: str, n: int = 5) -> list[str]:
    """Return the next N fire times for a given cron expression."""
    cron = CronExpression(expression)
    return [str(t) for t in cron.next_n_fire_times(n)]
:::

**Run it.** `CronExpression` needs no running app, so the fastest way to build
intuition is the REPL. From `samples/lumen`:

```bash
uv run python -c "from pyfly.scheduling import CronExpression; \
print(*CronExpression('0 2 * * *').next_n_fire_times(3), sep='\n')"
```

You should see the next three 02:00 instants, one per line (your dates will
differ):

```text
2026-06-16 02:00:00+00:00
2026-06-17 02:00:00+00:00
2026-06-18 02:00:00+00:00
```

Notice the `+00:00` — fire times are UTC unless you pass a `zone` (covered
next). This is also the cleanest way to sanity-check a user-supplied expression
before you store it: an invalid string raises `ValueError` immediately.

`CronExpression` accepts both the standard 5-field format (`min hour dom month dow`) and the Spring-style 6-field format with seconds first (`sec min hour dom month dow`). The Spring `?` wildcard is normalised to `*` transparently.

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

Cron expressions are evaluated in **UTC** by default. Pass `zone` with an IANA time-zone name to evaluate fire times in a specific zone instead:

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

DST transitions are handled by the `zoneinfo` database; no manual offset adjustment is required.

### Distributed locking

When multiple Lumen instances run behind a load balancer, every instance schedules the same `@scheduled` methods. Without coordination, the nightly rollup fires once per instance and writes duplicate records. The `lock` parameter solves this the same way Spring's `@SchedulerLock` (ShedLock) does: before each tick the scheduler tries to acquire a named lock and **skips the run** if the lock is already held elsewhere.

```python
@scheduled(cron="0 2 * * *", lock=True, lock_ttl=timedelta(minutes=5))
async def run(self) -> None:
    """lock=True auto-names the lock 'DailyRollupService.run'."""
    ...
```

- `lock=True` — derives the lock name from `"ClassName.method_name"`.
- `lock="shared-name"` — explicit name; useful when two methods must be mutually exclusive.
- `lock_ttl` — safety-valve TTL; set it comfortably longer than the job's worst-case runtime.

By default `TaskScheduler` uses `LocalLock`, which always acquires — single-instance behaviour is unchanged. For cross-process coordination, implement `DistributedLock` and register it as a bean:

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

`SchedulingAutoConfiguration` detects the `DistributedLock` bean in the container automatically and passes it to the `TaskScheduler`. Any object with conforming `try_acquire` and `release` coroutines satisfies the protocol.

### @async_method

**`@async_method`** marks a method for fire-and-forget execution via the `TaskExecutorPort`. The caller returns immediately; the framework routes the coroutine through the configured executor in the background:

```python
from pyfly.scheduling import async_method


@service
class AlertService:

    @async_method
    async def send_alert(self, msg: str) -> None:
        """Caller does not await — AlertService dispatches asynchronously."""
        ...
```

Under the hood `@async_method` sets `__pyfly_async__ = True` on the function; the framework detects this flag and submits the coroutine to the `TaskExecutorPort`.

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
    executor:
      type: asyncio        # 'asyncio' (default, in-loop) or 'thread'
      max-workers: 4       # worker threads when type is 'thread'
    lock:
      provider: none       # none | memory | redis | postgres
```

When `enabled` is `false`, `TaskScheduler` starts no loops and all `@scheduled` methods are silently skipped.

The `executor.type` chooses how each tick runs. The default `asyncio` runs the
coroutine on the application event loop — ideal for short, I/O-bound jobs like
the rollup. Switch to `thread` (a pool of `executor.max-workers` threads) when a
job does heavy CPU work or calls a blocking library, so it cannot stall the loop.

!!! tip "Choosing a lock provider"
    `lock.provider` selects the backend behind `@scheduled(lock=...)`, described
    next: `none` (the default — no coordination), `memory` (mutual exclusion
    within one process), `redis`, or `postgres` (true cross-instance
    coordination with no code change). On `redis`/`postgres` PyFly builds the
    `DistributedLock` bean for you from `pyfly.scheduling.lock.redis.url` or the
    app's existing `AsyncEngine`; the hand-rolled `@bean` in Listing 17.4 is the
    do-it-yourself alternative when you need custom semantics.

---

## Notifications

Lumen needs to tell customers that their money has arrived — email for the balance confirmation, and optionally an SMS or mobile push for the real-time alert.

PyFly's notifications module defines three **port protocols** and three **default services**. Your business logic depends on the protocols; the concrete provider adapters — SMTP, SendGrid, Twilio, Firebase — live behind the port boundary and can be swapped without touching a single line of domain code.

!!! note "New term: port and adapter"
    A *port* is an interface your code talks to — here, "something that can send
    an email". An *adapter* is a concrete implementation of that port —
    `SmtpEmailProvider`, `SendGridEmailProvider`, and so on. The pattern (also
    called *hexagonal architecture*) means your deposit logic depends only on the
    `EmailService` port, never on a specific vendor. Swapping SMTP for SendGrid
    is a one-line change in a configuration class; the domain code never notices.

### The port hierarchy

| Protocol | Service class | Method |
|---|---|---|
| `EmailProvider` | `DefaultEmailService` | `send(EmailMessage) -> NotificationResult` |
| `SmsProvider` | `DefaultSmsService` | `send(SmsMessage) -> NotificationResult` |
| `PushProvider` | `DefaultPushService` | `send(PushMessage) -> NotificationResult` |

`DefaultEmailService`, `DefaultSmsService`, and `DefaultPushService` are thin wrappers: each delegates to a provider, catches any provider exception, and returns a structured `NotificationResult` with `status=FAILED` and the error string rather than propagating the exception. A transient SendGrid outage does not take down the deposit handler.

### Messages and results

`FundsDeposited` carries `amount` and `balance` as **integer minor units** (cents). The `Money.major_units` property converts them for display — `Money(25000, Currency.EUR).major_units` is `250.0`. Keep that in mind when formatting notification bodies.

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
    data={"wallet_id": "w-001", "amount_minor": 25000},
)
:::

`NotificationResult` carries `id`, `provider`, `status` (`EmailStatus.SENT | DELIVERED | FAILED | ...`), an optional `provider_id` (e.g. the SendGrid message ID), and an optional `error`.

### Wiring the SMTP provider

For development and self-hosted deployments, `SmtpEmailProvider` runs `smtplib` from a thread pool so the async event loop is never blocked.

**Step 1 — Build the provider as a `@bean`.** A `@configuration` class is PyFly's
place to assemble objects the container cannot construct on its own — here, an
SMTP client that needs a host, credentials, and TLS settings. Each `@bean`
method returns one ready-to-use object; the framework caches it and injects it
wherever the return type is requested.

**Step 2 — Wrap it in `DefaultEmailService`.** The second `@bean` takes the
provider and returns it as an `EmailService` — the *port* your domain code
depends on. The declared return type matters: by returning `EmailService`, every
class that asks for an `EmailService` receives this wrapper, and none of them
learn that SMTP is behind it.

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

`SmtpEmailProvider` accepts `host`, `port` (default `587`), `username`, `password`, and `use_tls` (default `True`). Swap it for `SendGridEmailProvider` or `ResendEmailProvider` by changing one `@bean` method — `DefaultEmailService` is indifferent to the provider behind it.

!!! tip "Available providers"
    `pyfly.notifications` ships eight built-in adapters:
    `DummyEmailProvider` / `DummySmsProvider` / `DummyPushProvider`
    (log-only, for dev/tests), `SmtpEmailProvider`,
    `SendGridEmailProvider`, `ResendEmailProvider` (email),
    `TwilioSmsProvider` (SMS), and `FirebasePushProvider` (push). All
    satisfy their respective `*Provider` protocol.

### Sending a "funds received" notification

Lumen publishes a `FundsDeposited` domain event every time the `deposit()` command succeeds (see Chapter 8). The right place to trigger the notification is an EDA listener subscribed to that event — not the command handler itself, which keeps the deposit path free of notification concerns.

`FundsDeposited` carries `wallet_id: str`, `amount: int` (minor units), `currency: str`, and `balance: int` (new balance, minor units). The listener converts `amount` to a display string via `amount / 100`.

**Step 1 — Subscribe to the event.** Stack `@event_listener(event_types=["FundsDeposited"])`
on an `async` method of a `@service`. At startup PyFly discovers the stamped
method and auto-subscribes it to the `EventPublisher` bus — exactly the same
mechanism `WalletAuditListener` used back in Chapter 8. You never wire a bus by
hand.

**Step 2 — Read the payload.** The handler receives an `EventEnvelope`. Its
`payload` is a plain dict of the event's fields, so you pull `wallet_id`,
`amount`, `currency`, and `balance` out with `.get(...)` and coerce the amounts
to ints. Because amounts are minor units, dividing by 100 gives the display
value: `25000` becomes `250.00`.

**Step 3 — Send through the ports.** Inject `EmailService` and `PushService` in
the constructor and call `.send(...)` on each. Both return a `NotificationResult`
rather than raising — a flaky provider degrades gracefully instead of crashing
the listener.

::: listing lumen/wallet/deposit_notification_listener.py | Listing 17.7 — Notifying on FundsDeposited
from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener
from pyfly.notifications import (
    EmailMessage,
    EmailService,
    PushMessage,
    PushService,
)


@service
class DepositNotificationListener:
    """Sends email + push when a FundsDeposited event is observed."""

    def __init__(
        self,
        email_service: EmailService,
        push_service: PushService,
    ) -> None:
        self._email = email_service
        self._push = push_service

    @event_listener(event_types=["FundsDeposited"])
    async def on_funds_deposited(
        self, envelope: EventEnvelope,
    ) -> None:
        payload = envelope.payload
        wallet_id = str(payload.get("wallet_id", ""))
        amount_minor = int(payload.get("amount", 0))
        currency = str(payload.get("currency", "EUR"))
        balance_minor = int(payload.get("balance", 0))
        amount_str = f"{amount_minor / 100:.2f} {currency}"
        balance_str = f"{balance_minor / 100:.2f} {currency}"

        # Fetch contact details from a wallet profile service in prod;
        # hardcoded here for illustration.
        email = "customer@example.com"
        device_token = "FCM_TOKEN_GOES_HERE"

        await self._email.send(EmailMessage(
            to=[email],
            sender="no-reply@lumenbank.com",
            subject=f"Funds received: {amount_str}",
            body_text=(
                f"{amount_str} has been credited to wallet "
                f"{wallet_id}. New balance: {balance_str}."
            ),
        ))
        await self._push.send(PushMessage(
            device_tokens=[device_token],
            title="Funds received",
            body=f"{amount_str} credited",
            data={
                "wallet_id": wallet_id,
                "amount_minor": amount_minor,
                "currency": currency,
            },
        ))
:::

Both calls return a `NotificationResult`; inspect the `status` field to log failures or schedule retries.

**Run it.** You do not want a real SMTP server while developing, so swap the
provider for the log-only `DummyEmailProvider`. In your `@configuration` class,
return a `DummyEmailProvider` (and `DummyPushProvider`) instead of the SMTP one,
then start the app and trigger a deposit:

```bash
uv run pyfly run --server uvicorn
# in a second terminal, open a wallet and deposit (see Chapter 7), e.g.:
curl -s -X POST localhost:8080/api/v1/wallets/<wallet-id>/deposit \
  -H 'content-type: application/json' -d '{"amount":25000}'
```

The deposit publishes `FundsDeposited`, the listener fires, and the dummy
providers log the messages they "sent":

```text
[dummy email] to=['customer@example.com'] subject=Funds received: 250.00 EUR
[dummy push] tokens=1 title=Funds received
```

The `DummyEmailProvider` also keeps every message it received in a `.sent` list —
which is exactly what the test in Exercise 2 asserts against, no SMTP server
required.

!!! note "What just happened"
    The deposit command knew nothing about email. It simply did its job and
    raised a `FundsDeposited` domain event. The notification logic lives in a
    separate listener that *reacts* to that event, so the deposit path stays
    clean and you can add, remove, or change notifications without touching the
    command handler. That separation — publish a fact, let interested parties
    react — is the whole point of event-driven architecture.

!!! spring "Spring parity"
    `EmailService` / `SmsService` / `PushService` are the Python
    equivalents of Spring's `JavaMailSender` (email) and third-party
    Spring integrations for SMS and push. The hexagonal port/adapter
    split is identical: your domain code depends on the protocol;
    concrete provider adapters live in the infrastructure layer.

---

## Inbound webhooks

An illustrative payment provider POSTs a `payment_intent.succeeded` event to Lumen whenever a customer tops up their wallet by card. Lumen must:

1. **verify the HMAC-SHA256 signature** to reject forged payloads;
2. **deduplicate** replays using the idempotency key so a retry does not credit a wallet twice;
3. **dispatch** the verified event to a typed listener.

PyFly's `pyfly.webhooks` module handles all three steps.

!!! note "New terms: webhook, HMAC, idempotency"
    A *webhook* is an HTTP POST that an external system sends *to you* when
    something happens — the inbound mirror of the outbound callbacks later in
    this chapter. Because anyone can POST to a public URL, the provider signs
    each request with a shared secret using *HMAC* (a keyed hash); recomputing
    the hash over the exact bytes received and comparing proves the payload is
    genuine and untampered. *Idempotency* means "safe to receive more than
    once": providers retry on network blips, so you store an idempotency key and
    ignore a repeat — otherwise one card top-up could credit a wallet twice.

### WebhookEvent and AbstractWebhookEventListener

Every inbound event is modelled as a `WebhookEvent` dataclass:

```python
@dataclass
class WebhookEvent:
    id: str               # auto-generated UUID
    source: str           # e.g. "payment-provider"
    event_type: str       # from body["type"]
    headers: dict[str, str]
    body: dict[str, Any]
    raw_body: bytes
    received_at: datetime
    idempotency_key: str | None
```

Subclass `AbstractWebhookEventListener` and set `source` to the name
you will pass to `WebhookProcessor.process()`:

::: listing lumen/webhooks/payment_listener.py | Listing 17.8 — Payment-provider webhook listener
from pyfly.container import service
from pyfly.webhooks import AbstractWebhookEventListener, WebhookEvent


@service
class PaymentWebhookListener(AbstractWebhookEventListener):
    source = "payment-provider"

    def __init__(self, deposit_handler) -> None:
        self._handler = deposit_handler

    async def handle(self, event: WebhookEvent) -> None:
        if event.event_type == "payment_intent.succeeded":
            pi = event.body.get("data", {}).get("object", {})
            wallet_id = pi.get("metadata", {}).get("wallet_id")
            amount_minor = int(pi.get("amount_received", 0))
            currency_code = pi.get("currency", "EUR").upper()
            if wallet_id and amount_minor > 0:
                # Delegate to the CQRS command handler so the aggregate
                # enforces the balance invariant and raises FundsDeposited.
                from lumen.core.services.wallets.deposit_funds_command import (
                    DepositFunds,
                )
                await self._handler.handle(
                    DepositFunds(
                        wallet_id=wallet_id,
                        amount=amount_minor,
                    )
                )

    async def on_error(
        self, event: WebhookEvent, error: BaseException,
    ) -> None:
        # Override to DLQ or page on-call.
        pass
:::

`on_error` is called when `handle` raises; the default is a no-op. Override it to publish to a dead-letter queue or emit a metric.

### WebhookProcessor — verify, dedupe, dispatch

**`WebhookProcessor`** wires together a signature validator, an idempotency store, and a list of listeners. Assemble it in a `@configuration` class so it is a
single shared bean:

- `listeners` is the list of `AbstractWebhookEventListener` subclasses to fan
  events out to (just `PaymentWebhookListener` for now);
- `signature_validators` maps each `source` name to the validator that proves its
  requests are genuine — here an `HmacSignatureValidator` keyed by the webhook
  secret your provider gave you;
- an `event_store` (omitted here, so the default in-memory one is used) remembers
  idempotency keys.

::: listing lumen/webhooks/processor_config.py | Listing 17.9 — Assembling WebhookProcessor
from pyfly.container import bean, configuration
from pyfly.webhooks import (
    HmacSignatureValidator,
    WebhookProcessor,
)
from lumen.webhooks.payment_listener import PaymentWebhookListener


@configuration
class WebhookConfig:

    @bean
    def webhook_processor(
        self, payment_listener: PaymentWebhookListener,
    ) -> WebhookProcessor:
        return WebhookProcessor(
            listeners=[payment_listener],
            signature_validators={
                "payment-provider": HmacSignatureValidator(
                    secret="whsec_REPLACE_ME",
                ),
            },
        )
:::

`HmacSignatureValidator` expects the `sha256=<hex>` header format and uses `hmac.compare_digest` for a constant-time comparison. Change the `header_prefix` parameter if your provider uses a different scheme.

### Handling a webhook in an HTTP handler

Call `processor.process()` from your inbound HTTP handler. Pass the raw request body (unmodified bytes) — the validator computes the HMAC over the exact bytes received.

!!! warning "Read the body as raw bytes, not parsed JSON"
    The signature is computed over the *exact bytes* the provider sent. If you
    parse the JSON and re-serialize it, key order or whitespace can shift and the
    HMAC will no longer match — every legitimate request would be rejected.
    Always pass `await request.body()` (the untouched bytes) to `process()`, as
    the handler below does.

::: listing lumen/webhooks/payment_handler.py | Listing 17.10 — Inbound payment-provider webhook endpoint
from pyfly.container import rest_controller
from pyfly.web import post_mapping, request_mapping
from pyfly.webhooks import WebhookProcessor
from starlette.requests import Request
from starlette.responses import Response


@rest_controller
@request_mapping("/webhooks")
class PaymentWebhookHandler:

    def __init__(self, processor: WebhookProcessor) -> None:
        self._processor = processor

    @post_mapping("/payment")
    async def receive(self, request: Request) -> Response:
        # Read the untouched bytes — the HMAC is computed over exactly
        # what the provider sent (see the warning above).
        raw_body = await request.body()
        headers = {
            "X-Signature": request.headers.get(
                "X-Webhook-Signature", ""
            ),
            "X-Idempotency-Key": request.headers.get(
                "X-Idempotency-Key", ""
            ),
        }
        try:
            await self._processor.process(
                source="payment-provider",
                raw_body=raw_body,
                headers=headers,
            )
        except ValueError:
            return Response(content=b"invalid signature", status_code=400)
        return Response(content=b"ok", status_code=200)
:::

The `process()` signature accepts `signature_header` and `idempotency_header` keyword arguments to override the default header names (`X-Signature` and `X-Idempotency-Key`).

**What happens inside `process()`:**

1. The validator is looked up by `source`; if none is registered, a `NoOpSignatureValidator` is used — always passes, safe for development but not production.
2. If signature validation fails, `ValueError` is raised immediately and no listeners are called.
3. The raw body is decoded as JSON; on failure the raw bytes are stored under `body["_raw"]`.
4. If `idempotency_key` is present and already seen, the event is returned but listeners are **not** called.
5. Each listener for the source is called in registration order; if one raises, the error is logged and `on_error` is called before continuing to the next listener.

**Run it.** Test the endpoint the way a real provider would — by signing the exact
body. Pick the same secret you put in `HmacSignatureValidator` (`whsec_REPLACE_ME`
in Listing 17.9), compute the HMAC with `openssl`, and POST it. Start the app
(`uv run pyfly run --server uvicorn`), then in a second terminal:

::: listing terminal | Listing 17.10a — Sign and POST a webhook
BODY='{"type":"payment_intent.succeeded","data":{"object":{"amount_received":25000,"currency":"eur","metadata":{"wallet_id":"<wallet-id>"}}}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "whsec_REPLACE_ME" | sed 's/^.* //')

curl -s -X POST localhost:8080/webhooks/payment \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -H "X-Idempotency-Key: evt-001" \
  -H 'content-type: application/json' \
  -d "$BODY"
:::

A correctly signed request returns `ok` and credits the wallet (you will see the
`FundsDeposited` notification logs from earlier fire too):

```text
ok
```

Now prove the two guarantees. POST the **same** command again with the same
`X-Idempotency-Key` — it still returns `ok`, but the wallet is *not* credited a
second time (the duplicate is dropped before any listener runs). Then tamper with
one byte of `$BODY` *without* recomputing `$SIG` and POST again — the signature
no longer matches, so the handler returns:

```text
invalid signature
```

That is the verify-dedupe-dispatch pipeline working end to end, and it mirrors
Exercise 3 almost exactly.

!!! note "What just happened"
    A stranger on the internet POSTed JSON to your service, and three guards ran
    before a single line of your business logic: the signature check rejected
    forgeries, the idempotency store rejected replays, and only then did the
    typed listener translate the event into a CQRS `DepositFunds` command — so
    the wallet aggregate still enforced its own invariants. You wrote the
    `handle()` body; PyFly supplied the gauntlet around it.

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

When Lumen books a disbursement to a partner bank, that partner expects a `DisbursementSettled` POST to their webhook URL — signed, retried on failure, and auditable. PyFly's `pyfly.callbacks` module handles the outbound side.

!!! note "New term: outbound callback"
    A *callback* here is the reverse of the inbound webhook you just built:
    *Lumen* is now the sender, POSTing an event *to* a partner's URL. The
    module gives you the same trust and reliability machinery on the way out —
    it signs each payload (so the partner can verify it), retries transient
    failures with backoff, and records every attempt so you have an audit trail
    when a partner asks "did you ever tell us about transaction X?".

### Subscriptions and config

Each partner is modelled as a **`CallbackConfig`** — a tenant-scoped record that holds the webhook secret, event subscriptions, and retry policy.

!!! note "New term: tenant"
    A *tenant* is one isolated customer or organisation inside a shared
    application — here, `"lumen"`. Callbacks are tenant-scoped so a multi-tenant
    deployment can hold each tenant's partner URLs, secrets, and retry policy
    separately and never cross the wires. With a single tenant you simply pass
    the same `tenant_id` everywhere.

**Step 1 — Describe each subscription.** A `CallbackSubscription` pairs an
`event_type` with the `target_url` to POST it to. Use the exact event name
(`"DisbursementSettled"`) to route one event, or `"*"` as a catch-all that
matches every event for the tenant — handy for an audit endpoint that wants the
full firehose.

**Step 2 — Wrap them in a `CallbackConfig` and save it.** The config carries the
shared `secret` (used to sign every payload), the retry policy (`max_attempts`,
`backoff_ms`), and the list of subscriptions. Persist it through a
`CallbackConfigRepository` — `InMemoryCallbackConfigRepository` for now, a
database-backed one in production.

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

`event_type="*"` is a catch-all: every event dispatched for the tenant matches. Named types match only the exact event type string.

### Dispatching an event

**`CallbackDispatcher.dispatch()`** fans the event out to every matching subscription:

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

Then in the domain service — note the payload uses `amount` in minor units (cents), so `50_000` is EUR 500.00:

```python
results = await dispatcher.dispatch(
    "lumen",            # tenant_id
    "DisbursementSettled",
    {"id": "txn-009", "amount": 50_000, "currency": "EUR"},
)
```

`dispatch()` returns one `CallbackExecution` record per matching subscription, each with `status`, `attempts`, `response_status`, and `delivered_at`.

**Run it.** The dispatcher's default HTTP sender does not actually call the
network — it logs the request it *would* make and returns `200` — which is
perfect for seeing the wiring without standing up a partner server. Drop this
into a script (or `uv run python`) from `samples/lumen`:

::: listing lumen/callbacks/try_dispatch.py | Listing 17.12a — Dispatch against the default (log-only) sender
import asyncio

from pyfly.callbacks import (
    CallbackConfig,
    CallbackDispatcher,
    CallbackSubscription,
    InMemoryCallbackConfigRepository,
    InMemoryCallbackExecutionRepository,
)


async def main() -> None:
    configs = InMemoryCallbackConfigRepository()
    await configs.save(CallbackConfig(
        tenant_id="lumen",
        name="clearance-bank",
        secret="cb-secret-xyz",
        subscriptions=[CallbackSubscription(
            event_type="DisbursementSettled",
            target_url="https://api.clearancebank.example.com/hooks/lumen",
        )],
    ))
    dispatcher = CallbackDispatcher(
        configs=configs,
        executions=InMemoryCallbackExecutionRepository(),
    )
    results = await dispatcher.dispatch(
        "lumen",
        "DisbursementSettled",
        {"id": "txn-009", "amount": 50_000, "currency": "EUR"},
    )
    for r in results:
        print(r.status, r.attempts, r.response_status, r.target_url)


asyncio.run(main())
:::

You should see one delivered execution, with the signed request logged just
above it:

```text
would POST https://api.clearancebank.example.com/hooks/lumen headers={'X-Pyfly-Signature': 'sha256=...', 'Content-Type': 'application/json'} body={'id': 'txn-009', 'amount': 50000, 'currency': 'EUR'}
DELIVERED 1 200 https://api.clearancebank.example.com/hooks/lumen
```

`DELIVERED 1 200` reads as: status `DELIVERED`, succeeded on attempt `1`, HTTP
`200`. To send for real, pass your own `http=` sender (an `httpx`/`aiohttp` POST)
to `CallbackDispatcher`; the signing, retry, and audit logic stay identical.

!!! note "What just happened"
    One `dispatch()` call looked up every subscription the tenant has for that
    event type, signed the payload, POSTed it, and wrote a `CallbackExecution`
    record for each — all without your domain service knowing how many partners
    are listening or how retries work. Adding a partner later is just another
    saved `CallbackConfig`; the disbursement code never changes.

### HMAC signing and retry logic

When `CallbackConfig.secret` is set, `CallbackDispatcher` signs the canonical JSON payload before every POST using HMAC-SHA256:

```python
# canonical body — compact, keys sorted
canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
sig = hmac.new(secret, canonical.encode(), hashlib.sha256).hexdigest()
headers["X-Pyfly-Signature"] = f"sha256={sig}"
```

The recipient can verify the signature using PyFly's own `HmacSignatureValidator` — the same class used for inbound webhooks.

**Retry policy:**

- The dispatcher retries up to `max_attempts` times (default `5`).
- Between retries it applies exponential backoff: `delay = min(backoff_ms * 2^(attempt-1), 300_000) ms`.
- Only *transient* HTTP status codes trigger a retry: `408`, `429`, `500`, `502`, `503`, `504`, or any `>= 500`.
- Permanent client errors (`4xx` except `408`/`429`) mark the execution as `FAILED` immediately without retrying.
- On success (`2xx`) the execution is marked `DELIVERED` and `delivered_at` is stamped.

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

The `authorized_domains` field on `CallbackConfig` acts as an allowlist. When set, `CallbackDispatcher` checks that the target URL's hostname matches one of the allowed domains before making any outbound request. A URL that fails the check is immediately marked `FAILED` with `last_error="Domain not authorized"` — no HTTP request is made.

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

!!! note "New term: SSRF"
    *Server-Side Request Forgery* is an attack where a malicious value tricks
    your server into making an HTTP request it should not — for example, a
    partner URL pointing at `http://169.254.169.254/` (a cloud metadata
    endpoint) to steal credentials. Because callback URLs can come from
    partner-supplied config, the `authorized_domains` allowlist closes that door:
    a host that is not on the list is marked `FAILED` *before* any request
    leaves the process. To verify, add an `AuthorizedDomain` for one host, then
    dispatch a subscription whose `target_url` points elsewhere — the returned
    `CallbackExecution` will read `status=FAILED`, `attempts=0`, and
    `last_error="Domain not authorized"`, and nothing is sent.

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

**Run it — confirm the suite is still green.** You added four new integration
patterns; make sure nothing else regressed. From the `samples/lumen` directory:

```bash
uv run --extra dev pytest -q
```

You should see every existing test still pass:

```text
.........................................                                [100%]
41 passed in 0.28s
```

The three exercises below add scheduling, notification, and webhook tests of
their own — re-run this command after each to watch the count climb. Remember the
`--extra dev` flag; the bare `uv sync` omits pytest.

---

## What you built {.recap}

You extended Lumen into a connected system that operates independently of incoming requests:

- **Scheduled tasks** — `@scheduled` with `cron`, `fixed_rate`, and `fixed_delay` triggers runs work on a calendar or timer. `CronExpression` drives fire-time calculations, including time-zone-aware scheduling with DST handled automatically. `lock=True` serialises cluster-wide execution via the `DistributedLock` port. `@async_method` offloads fire-and-forget work to the executor.

- **Notifications** — `EmailService`, `SmsService`, and `PushService` port protocols decouple business logic from provider adapters (SMTP, SendGrid, Resend, Twilio, Firebase). `DefaultEmailService` and its siblings catch provider errors and return structured `NotificationResult` values rather than propagating exceptions. `DepositNotificationListener` subscribes to the real `FundsDeposited` EDA event and converts minor-unit amounts to display strings before sending.

- **Inbound webhooks** — `AbstractWebhookEventListener` defines typed consumers. `WebhookProcessor` gates every event with `HmacSignatureValidator` and `WebhookEventStore` before dispatching to listeners. `on_error()` hooks allow DLQ integration without breaking the dispatch loop.

- **Outbound callbacks** — `CallbackDispatcher` fans events out to `CallbackSubscription` targets, signs payloads with HMAC-SHA256 under the `X-Pyfly-Signature` header, and retries on transient failures with exponential backoff. `CallbackExecution` records provide a full delivery audit trail. `authorized_domains` prevents SSRF.

---

## Try it yourself {.exercises}

1. **Cluster lock.** Add a second instance of `DailyRollupService` to a
   test that spins up two `ApplicationContext` instances sharing a
   `FakeDistributedLock` (one that only returns `True` for the first
   acquirer). Assert that `run()` is called exactly once across both
   instances for a single cron tick.

2. **Provider swap.** Replace `SmtpEmailProvider` with a
   `DummyEmailProvider` in the test suite. Write a test that deposits
   EUR 100 (10 000 minor units) into wallet `w-001` via the deposit
   handler, triggering a `FundsDeposited` event. The provider records
   every message it received in its `.sent` list, so assert that
   `provider.sent[-1].body_text` contains the wallet ID and the
   formatted amount (`100.00 EUR`).

3. **Signature replay attack.** Write a test that calls
   `PaymentWebhookHandler.receive()` twice with the same raw body,
   headers, and idempotency key. Assert that the first call returns 200
   and credits the wallet (triggering `FundsDeposited`), and the second
   call also returns 200 (the duplicate is silently ignored by
   `InMemoryWebhookEventStore`) but does **not** credit the wallet a
   second time.
