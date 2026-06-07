<span class="eyebrow">Chapter 8</span>

# Domain Events & Event-Driven Architecture {.chtitle}

::: figure art/openers/ch08.svg | &nbsp;

Lumen's wallet saves correctly, validates rigorously, and now dispatches every write through a typed command. But look at what the command handlers do after they call `repo.save`: nothing. The domain events that the `Wallet` aggregate buffers — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are drained and discarded. The bus pipeline that Chapter 7 promised would "publish domain events" has nowhere to send them yet.

The gap is real. The team at Lumen wants a balance read model that always stays in sync without reloading the aggregate on every query. They want a welcome notification when a new wallet opens. They want an immutable audit trail of every financial movement for compliance. All three of those features depend on knowing *that something happened* — not who asked for it to happen, and not how it was done.

Domain events are the answer. Rather than having the command handler call the notification service directly, or coupling the audit log to the repository, you publish the event — a concise, timestamped, immutable record of a fact — onto an event bus, and every interested party subscribes independently. The handler that saved the wallet does not need to know what the notifier does, or even that the notifier exists. You can add a new subscriber months later without touching a single line of the existing handlers.

This chapter builds the reaction side of Lumen's architecture. You will wire `EventPublisher` into the command handlers, define an `EventEnvelope` for each wallet fact, and write three independent listeners: a `BalanceProjection` that keeps a fast read model, a `Notifier` that fires a welcome message, and an `AuditLog` that timestamps every movement. By the end of the chapter the write path and the read infrastructure will be fully decoupled — each can evolve without the other noticing.

---

## Two kinds of events

Before you touch any code, it helps to be precise about what "event" means in PyFly, because the framework uses the word for two distinct things — and confusing them leads to the wrong bus, the wrong subscription API, and subtle runtime surprises.

**Application events** (`pyfly.context.events`) are framework lifecycle notifications: `ContextRefreshedEvent` fires when the dependency-injection container has finished wiring all beans; `ApplicationReadyEvent` fires when the HTTP server is accepting connections; `ContextClosedEvent` fires during shutdown. These events are dispatched by the `ApplicationEventBus`, which matches subscribers by Python class type rather than string pattern. They are framework plumbing — useful for starting background workers or seeding caches on startup, but deliberately separate from any business concept.

**Domain events** (`pyfly.eda`) are business-level facts: *a wallet was opened*, *funds were deposited*, *a transfer was completed*. These events are wrapped in an `EventEnvelope` that pairs the payload with rich metadata and published through the `EventPublisher` port. Subscriptions are matched by pattern strings such as `"wallet.opened"` or `"wallet.*"`. They are the subject of this chapter.

The distinction matters because the bus you choose changes what you can do with the events. The `ApplicationEventBus` dispatches to Python callables keyed by class; `InMemoryEventBus` routes by glob pattern and can be swapped for a Kafka-backed adapter without touching subscriber code. Keep the two worlds separate: use lifecycle events for infrastructure bootstrapping, and domain events for everything that has business meaning.

!!! note "Application events are still useful"
    If you need to warm a cache as soon as the application is ready, `@app_event_listener` on `ApplicationReadyEvent` is the right tool. The two systems coexist; you can use both in the same service.

---

## Publishing events

### The EventPublisher port

The first question a new team member usually asks is: "which class do I import to fire an event?" The answer, deliberately, is not a class — it is a protocol. `EventPublisher` is a *port* in the hexagonal-architecture sense: any code that needs to publish an event depends on this interface, and the bus implementation is injected from outside. That design decision is what lets you run `InMemoryEventBus` locally today and swap in a Kafka adapter in production without touching a single handler.

The protocol exposes two methods:

```python
from pyfly.eda import EventPublisher

class EventPublisher(Protocol):
    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None: ...

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict,
        headers: dict[str, str] | None = None,
    ) -> None: ...
```

`publish` wraps your data in an `EventEnvelope` before delivery; you never construct the envelope manually when calling the publisher. `subscribe` is how you register handlers programmatically — though in practice you will use the `@event_listener` decorator rather than calling `subscribe` directly, because the decorator lets the `ApplicationContext` wire subscriptions automatically at startup.

### The EventEnvelope

Every domain event reaches its listeners wrapped in an `EventEnvelope`. Think of the envelope as the metadata layer that transforms a bare Python dictionary into a traceable, auditable, first-class fact. It is a frozen dataclass — immutable once created — that pairs the payload with the context every listener needs to do its job correctly.

| Field | Type | Default | Description |
|---|---|---|---|
| `event_type` | `str` | required | Dot-separated identifier, e.g. `"wallet.opened"`. Used for pattern matching. |
| `payload` | `dict[str, Any]` | required | The event data. |
| `destination` | `str` | required | Logical channel or topic. |
| `event_id` | `str` | auto UUID | Unique ID for this event instance. |
| `timestamp` | `datetime` | `datetime.now(UTC)` | UTC creation time. |
| `headers` | `dict[str, str]` | `{}` | Arbitrary metadata: correlation IDs, trace context, and so on. |

Three of those fields deserve special attention. The `event_id` is a stable UUID generated by the bus at publish time — it is your idempotency key for exactly-once semantics, available in every listener with no extra effort. The `timestamp` records when the fact was observed, not when the listener processes it, so it remains accurate even if a listener runs with a delay. The `headers` dict carries cross-cutting concerns such as distributed trace IDs that have nothing to do with the business payload but matter enormously for observability. Because the envelope is frozen, handlers can safely pass it between coroutines without defensive copying.

### Wiring the publisher into the command handlers

In Chapter 7 the command handlers loaded aggregates, drove domain behaviour, and saved — leaving the buffered events on the floor. The aggregate was dutifully recording facts that nobody was listening to. Now you close that gap: inject an `EventPublisher` alongside the repository and, after a successful save, drain the aggregate's event buffer and publish each event in turn.

Here is an updated `DepositFundsHandler` that publishes the wallet's pending events:

::: listing lumen/cqrs/handlers/deposit_funds_handler.py | Listing 8.1 — DepositFundsHandler publishes the aggregate's buffered events
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher

from lumen.cqrs.commands import DepositFunds
from lumen.domain.money import Money
from lumen.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, None]):
    """Credit funds and publish the resulting domain events."""

    def __init__(
        self,
        repo: WalletDomainRepository,
        publisher: EventPublisher,
    ) -> None:
        self._repo = repo
        self._publisher = publisher

    async def do_handle(self, command: DepositFunds) -> None:
        wallet = await self._repo.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(amount=command.amount_cents, currency=command.currency))
        await self._repo.save(wallet)

        # Drain the aggregate's event buffer and publish each event.
        for event in wallet.clear_events():
            await self._publisher.publish(
                destination="wallet",
                event_type=f"wallet.{type(event).__name__.lower()}",
                payload=event.__dict__,
            )
:::

There are two design decisions worth noting here. First, `publisher: EventPublisher` is typed as the protocol, not as `InMemoryEventBus`. The DI container injects whichever implementation is registered — the handler never knows or cares which one. Second, the publish loop sits *after* `self._repo.save(wallet)`. That ordering is intentional and important: if the save fails, no event is published — listeners never see a fact that did not actually persist. If the publish fails after a successful save you have an at-least-once delivery challenge, which Chapter 10 will address with transactional outbox patterns. For now, the in-memory bus does not fail.

The `OpenWalletHandler` follows the same pattern:

::: listing lumen/cqrs/handlers/open_wallet_handler_v2.py | Listing 8.2 — OpenWalletHandler publishes WalletOpened after saving
from __future__ import annotations

from pyfly.container import service
from pyfly.cqrs.command.handler import CommandHandler
from pyfly.cqrs.decorators import command_handler
from pyfly.eda import EventPublisher

from lumen.cqrs.commands import OpenWallet
from lumen.domain.wallet import Wallet
from lumen.domain.wallet_repository import WalletDomainRepository


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new wallet and publish WalletOpened."""

    def __init__(
        self,
        repo: WalletDomainRepository,
        publisher: EventPublisher,
    ) -> None:
        self._repo = repo
        self._publisher = publisher

    async def do_handle(self, command: OpenWallet) -> str:
        wallet = Wallet.open(owner_id=command.owner_id, currency=command.currency)
        await self._repo.save(wallet)
        assert wallet.id is not None

        for event in wallet.clear_events():
            await self._publisher.publish(
                destination="wallet",
                event_type=f"wallet.{type(event).__name__.lower()}",
                payload=event.__dict__,
            )

        return wallet.id
:::

Notice that `return wallet.id` comes *after* the publish loop. That is deliberate: the handler fulfils its contract (returning the new wallet ID to the caller) only once every fact produced by the operation has been dispatched. The `assert wallet.id is not None` on the line before is a sanity guard that confirms the repository populated the ID during save — without it, a bug in the repository could silently publish an envelope with a `None` wallet ID.

### The @publish_result shortcut

For simpler cases where a method's return value *is* the event payload — common in lightweight services that have not adopted the full aggregate pattern — `@publish_result` removes the manual publish call entirely:

::: listing lumen/eda/publish_result_example.py | Listing 8.3 — @publish_result auto-publishes the method's return value
from pyfly.eda import publish_result
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()


@publish_result(bus, destination="wallet", event_type="wallet.credited")
async def credit_wallet(wallet_id: str, amount_cents: int, currency: str) -> dict:
    # Business logic omitted — the returned dict IS the event payload.
    return {
        "wallet_id": wallet_id,
        "amount_cents": amount_cents,
        "currency": currency,
    }
:::

When `credit_wallet` returns, the decorator intercepts the result and calls `bus.publish` with it as the payload — no boilerplate loop required. The event's `destination` and `event_type` are fixed at decoration time, which keeps the business function clean. `@publish_result` also accepts an optional `condition` predicate so the event is only published when the result satisfies a test — useful for conditional workflows where not every successful execution should broadcast.

::: figure art/figures/08-eda.svg | Figure 8.1 — One publisher, many independent listeners.

!!! spring "Spring parity"
    `EventPublisher` is PyFly's counterpart of Spring's `ApplicationEventPublisher`. Calling `publisher.publish(...)` is equivalent to `applicationEventPublisher.publishEvent(event)`. The `@event_listener` decorator (next section) mirrors Spring's `@EventListener` for synchronous, same-transaction reactions and `@TransactionalEventListener` (with `phase = AFTER_COMMIT`) for reactions that should run only after the write is committed. `@publish_result` has no direct Spring equivalent but achieves what Spring developers often wire manually with AOP `@AfterReturning` advice.

---

## Reacting with @event_listener

Publishing an event is only half the equation. An event that nobody reacts to is just a log entry. The value of the event-driven model comes from the *reactions* it enables — independent pieces of behaviour that activate in response to the same published fact, each unaware of the others.

PyFly's `@event_listener` decorator is the simplest way to register a listener: decorate any async function with the patterns it cares about, and the `ApplicationContext` wires the subscription during startup — no bus reference required at decoration time.

```python
from pyfly.eda import event_listener, EventEnvelope

@event_listener(["wallet.opened"])
async def on_wallet_opened(envelope: EventEnvelope) -> None:
    ...
```

The pattern list supports globs: `["wallet.*"]` matches every event whose type starts with `"wallet."`. Because pattern matching happens at the bus level — not inside your function — a single listener method can cover an entire family of events without a single `if` branch.

### BalanceProjection

Every time a `GetBalance` query ran in Chapter 7, the handler loaded the full `Wallet` aggregate from the database, walked its event history, and recomputed the balance — even if nothing had changed since the last call. On a wallet with thousands of transactions, that cost adds up quickly.

A *projection* is the event-driven solution. Instead of reconstructing state on demand, you maintain a separate, denormalised read model that is kept in sync by events as they arrive. The `GetBalance` query handler then reads a single pre-computed row — an operation that is fast regardless of how many transactions the wallet has processed.

`BalanceProjection` subscribes to `wallet.fundsdeposited` and `wallet.fundswithdrawn` and keeps a `balance_cents` row up to date:

::: listing lumen/eda/balance_projection.py | Listing 8.4 — BalanceProjection maintains a fast read model from domain events
from __future__ import annotations

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener


@service
class BalanceProjection:
    """
    Keeps a denormalised balance table in sync by reacting to wallet events.
    In production, _store would be a real database session; here we use a dict
    for clarity.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    @event_listener(["wallet.fundsdeposited", "wallet.fundswithdrawn"])
    async def on_balance_changed(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload
        wallet_id: str = payload["wallet_id"]
        new_balance: int = payload["new_balance"]
        currency: str = payload["currency"]

        self._store[wallet_id] = {
            "wallet_id": wallet_id,
            "balance_cents": new_balance,
            "currency": currency,
            "updated_at": envelope.timestamp.isoformat(),
        }

    async def get(self, wallet_id: str) -> dict | None:
        return self._store.get(wallet_id)
:::

The listener receives an `EventEnvelope` from the bus. It reads the payload fields that were put there by `wallet.deposit` — `wallet_id`, `new_balance`, `currency` — and upserts the read-model row. Look at what is absent: no import of the `Wallet` aggregate, no call to any repository, no knowledge of how the deposit was processed. The projection reacts purely to the published fact. Both `fundsdeposited` and `fundswithdrawn` events land in the same `on_balance_changed` handler, which is possible because both carry the same shape of payload — the domain model was designed with that consistency in mind.

!!! tip "Envelope metadata in projections"
    The `envelope.timestamp` gives you the authoritative event time — when the fact was recorded, not when the listener ran. Store it in your read model and you get a cheap `updated_at` column for free, with no clock skew between writer and reader.

### Notifier

Consider what a "welcome notification" requires in a traditionally layered system: the HTTP handler creates the wallet, then calls a notification service, which must somehow be injected into the same request context. Any failure in the notification path can roll back the wallet creation, or you add complex error-swallowing logic. Neither is clean.

The event-driven approach is simpler. The `OpenWalletHandler` publishes `wallet.walletopened` after saving, and the `Notifier` reacts to that event in its own isolated path. The two are decoupled by design — the command handler's job is complete the moment the event is on the bus.

::: listing lumen/eda/notifier.py | Listing 8.5 — Notifier sends a welcome message when a wallet is opened
from __future__ import annotations

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener


@service
class Notifier:
    """Sends notifications in response to wallet domain events."""

    @event_listener(["wallet.walletopened"])
    async def on_wallet_opened(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload
        owner_id: str = payload.get("owner_id", "")
        wallet_id: str = payload.get("wallet_id", "")
        currency: str = payload.get("currency", "")

        # In production this would call an email / push-notification service.
        print(
            f"[Notifier] Welcome, {owner_id}! "
            f"Your {currency} wallet {wallet_id} is ready."
        )
:::

The `.get("owner_id", "")` pattern instead of direct key access is deliberate defensive coding: if a future version of the `WalletOpened` domain event changes its field names, the notifier degrades gracefully rather than crashing with a `KeyError` mid-flight. In a production notifier you would replace the `print` call with a call to your email or push-notification SDK — the surrounding structure stays exactly the same.

### AuditLog

Compliance regulations typically demand that every financial movement be logged immutably with a timestamp, an event type, and a correlation ID. In a synchronous system, you would have to thread an audit-logger through every service method that touches money — a pervasive cross-cutting concern. With domain events, the audit log writes itself.

The `AuditLog` catches every wallet event with a wildcard pattern:

::: listing lumen/eda/audit_log.py | Listing 8.6 — AuditLog captures every wallet event for compliance
from __future__ import annotations

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener


@service
class AuditLog:
    """Appends an entry to the audit trail for every wallet event."""

    @event_listener(["wallet.*"])
    async def record(self, envelope: EventEnvelope) -> None:
        print(
            f"[Audit] id={envelope.event_id} "
            f"ts={envelope.timestamp.isoformat()} "
            f"type={envelope.event_type} "
            f"dest={envelope.destination} "
            f"payload={envelope.payload}"
        )
:::

The `wallet.*` pattern matches `wallet.walletopened`, `wallet.fundsdeposited`, and `wallet.fundswithdrawn` in one subscription. New event types whose names start with `wallet.` are captured automatically — the `AuditLog` never needs editing. Every `envelope.event_id` is a stable UUID that can serve as the primary key in a compliance database; `envelope.timestamp` gives the authoritative event time; and `envelope.destination` records the logical channel, which maps to a Kafka topic or RabbitMQ exchange when you swap in a broker-backed bus.

What makes the overall design compelling is that adding these three listeners required zero changes to the command handlers, the `Wallet` aggregate, or the repositories. The `DepositFundsHandler` from Listing 8.1 has no idea that a projection exists. The `Notifier` has no idea that a handler exists. They are entirely independent — each is a consequence of the same published fact, wired together only by the bus.

---

## When listeners fail: error strategies

A listener that misbehaves raises a difficult question: should the failure stop the entire delivery chain, or should the bus continue notifying the remaining listeners? The right answer depends on the listener's role, and PyFly gives you explicit control over that choice rather than imposing a single policy for all cases.

In the default configuration the `InMemoryEventBus` invokes listeners sequentially and propagates any exception. For most development scenarios that is exactly what you want — a failing listener surfaces loudly. In production you often need finer control.

`ErrorStrategy` is an enum that governs how the bus behaves when a listener raises an exception:

```python
from pyfly.eda import ErrorStrategy
```

| Member | Value | Behaviour |
|---|---|---|
| `IGNORE` | `"IGNORE"` | Silently swallow the exception. Processing continues with the next handler. |
| `LOG_AND_CONTINUE` | `"LOG_AND_CONTINUE"` | Log the error then continue. The safest default for non-critical listeners. |
| `RETRY` | `"RETRY"` | Re-attempt delivery. Retry count and back-off are configured separately. |
| `DEAD_LETTER` | `"DEAD_LETTER"` | Move the failed event to a dead-letter destination for later inspection. |
| `FAIL_FAST` | `"FAIL_FAST"` | Propagate the exception immediately. No further handlers are invoked. |

You attach a strategy when registering the listener or when constructing the bus:

```python
@event_listener(["wallet.*"])
async def record(envelope: EventEnvelope) -> None:
    ...
```

The three Lumen listeners from this chapter each warrant a different strategy, which illustrates the principle well.

!!! tip "Match strategy to listener criticality"
    The `AuditLog` should use `LOG_AND_CONTINUE` — a broken audit logger must not halt a financial transaction. The `BalanceProjection`, which drives query responses, might warrant `RETRY` to ensure the read model stays consistent. The `Notifier` can tolerate `IGNORE` since a missed welcome email is not a data integrity issue.

!!! warning "Side effects and idempotency"
    If a listener performs a side effect — writing a database row, sending an email — and the bus retries delivery after a transient failure, the effect can run more than once. Design listeners to be idempotent: write a row only if the `event_id` has not already been recorded, send an email only if the welcome flag is not already set. The `envelope.event_id` (a stable UUID generated by the bus) is your idempotency key.

---

## In-memory today, a broker tomorrow

The `InMemoryEventBus` is the out-of-the-box implementation and the default that the `ApplicationContext` provides when you inject `EventPublisher`. It runs entirely in-process: `publish` is a direct async function call, there is no serialization, and if the process dies any un-delivered events are lost. That is perfectly acceptable for local development, integration tests, and monoliths that do not need cross-process delivery.

Understanding how the in-memory bus works internally makes it easier to reason about behaviour at the edges — and to appreciate exactly what changes when you swap in a broker.

```python
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()

bus.subscribe("wallet.*", my_handler)

await bus.publish(
    destination="wallet",
    event_type="wallet.fundsdeposited",
    payload={"wallet_id": "w-001", "amount": 5000, "currency": "EUR", "new_balance": 5000},
)
```

When you call `publish`, the bus executes four steps in sequence:

1. Wraps the arguments in an `EventEnvelope` with a generated `event_id` and a UTC `timestamp`.
2. Iterates every registered `(pattern, handler)` pair.
3. For each pair where `fnmatch.fnmatch(event_type, pattern)` is `True`, invokes the handler with the envelope.
4. Handlers are called sequentially in subscription order.

Subscriptions use Python's `fnmatch` under the hood, so `"wallet.*"` matches `"wallet.opened"` but not `"wallet.opened.extra"`, and `"*"` matches everything. The sequential invocation in step 4 means listener order is deterministic — useful in tests, but it also means a slow listener delays all subsequent ones. A broker-backed adapter typically dispatches to topic subscribers in parallel; keep that difference in mind when reasoning about throughput.

Because every listener in Lumen depends on the `EventPublisher` *protocol*, not on `InMemoryEventBus` directly, the implementation can be replaced without touching a single listener. Chapter 10 introduces Kafka and RabbitMQ adapters; swapping in either adapter is a configuration change — the `BalanceProjection`, `Notifier`, and `AuditLog` you wrote in this chapter will keep working without modification.

!!! note "InMemoryEventBus and testing"
    `InMemoryEventBus` is also the right tool for tests. Inject a fresh `InMemoryEventBus` as a fixture, subscribe a capturing handler, exercise your command handler, and assert on the `EventEnvelope` objects the handler received — including `event_type`, `payload`, `event_id`, and `timestamp`. No mocking, no fakes, just the real bus with controlled inputs.

---

## What you built {.recap}

Part III is open.

This chapter closed the loop that Chapter 7 began. The `Wallet` aggregate raised domain events in Chapter 6; the command handlers published them here; and three independent listeners — `BalanceProjection`, `Notifier`, `AuditLog` — react to those facts without knowing anything about each other or about the command path that triggered them.

The architecture is now genuinely event-driven within a single process. `EventPublisher` is the port — a protocol that any bus implementation can fulfil. `InMemoryEventBus` is the default adapter — entirely in-process, zero configuration, safe for development and tests. `EventEnvelope` carries the payload alongside `event_id`, `timestamp`, `destination`, and `headers` so every listener has the metadata it needs without querying additional services. `@event_listener` is the subscription decorator — pass a list of patterns, and the context wires the subscription at startup. `@publish_result` collapses the publish boilerplate when a method's return value is the event. `ErrorStrategy` gives you control over what happens when a listener fails, from `IGNORE` to `RETRY` to `DEAD_LETTER`.

Three design principles carry forward: **save before you publish**, so listeners never see uncommitted facts; **design listeners for idempotency**, so retries are safe; **depend on the port, not the adapter**, so the bus can be swapped without listener changes.

Chapter 9 pushes the event idea further: instead of maintaining a separate read model alongside a mutable aggregate, you store the events themselves as the system of record — event sourcing the Wallet ledger so that every historical balance is computable from first principles.

---

## Try it yourself {.exercises}

1. **Add a `FraudDetector` listener.** Create a `FraudDetector` service that subscribes to `"wallet.fundsdeposited"`. If the `amount_cents` in the payload exceeds `1_000_000` (ten thousand euros in minor units), log a warning that includes the `envelope.event_id`, `envelope.timestamp`, and the `wallet_id` from the payload. Verify it fires by publishing a `wallet.fundsdeposited` event directly to an `InMemoryEventBus` in a unit test, and assert that the warning was triggered.

2. **Use `@publish_result` on a transfer summary.** Add a `summarise_transfer` coroutine that accepts `source_id`, `target_id`, `amount_cents`, and `currency` and returns a dict with those four keys plus a `"status": "COMPLETED"` field. Decorate it with `@publish_result(bus, destination="wallet", event_type="wallet.transfercompleted")`. Write a test that subscribes a capturing handler to `"wallet.transfercompleted"`, calls `summarise_transfer`, and asserts that the captured envelope's `payload` matches the returned dict.

3. **Observe error strategy behaviour.** Copy `AuditLog` and make its `record` method raise `RuntimeError("audit failure")` unconditionally. Run it against an `InMemoryEventBus` configured with `ErrorStrategy.LOG_AND_CONTINUE` and confirm that a second listener (a simple list-appending handler registered for `"wallet.*"`) still receives the event despite the audit failure. Then switch the strategy to `ErrorStrategy.FAIL_FAST` and confirm that the second listener does *not* receive the event.
