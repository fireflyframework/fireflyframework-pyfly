<span class="eyebrow">Chapter 8</span>

# Domain Events & Event-Driven Architecture {.chtitle}

::: figure art/openers/ch08.svg | &nbsp;

Lumen's wallet saves correctly, validates rigorously, and now dispatches every write through a typed command. But look at what the command handlers do after they call `repo.save`: nothing. The domain events that the `Wallet` aggregate buffers — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are drained and discarded. The bus pipeline that Chapter 7 promised would "publish domain events" has nowhere to send them yet.

The gap is real. The team at Lumen wants a balance read model that always stays in sync without reloading the aggregate on every query. They want a welcome notification when a new wallet opens. They want an immutable audit trail of every financial movement for compliance. All three of those features depend on knowing *that something happened* — not who asked for it to happen, and not how it was done.

Domain events are the answer. Rather than having the command handler call the notification service directly, or coupling the audit log to the repository, you publish the event — a concise, timestamped, immutable record of a fact — onto an event bus, and every interested party subscribes independently. The handler that saved the wallet does not need to know what the notifier does, or even that the notifier exists. You can add a new subscriber months later without touching a single line of the existing handlers.

This chapter builds the reaction side of Lumen's architecture. You will wire `EventPublisher` into the command handlers, define an `EventEnvelope` for each wallet fact, and write three independent listeners: a `BalanceProjection` that keeps a fast read model, a `Notifier` that fires a welcome message, and an `AuditLog` that timestamps every movement. By the end of the chapter the write path and the read infrastructure will be fully decoupled — each can evolve without the other noticing.

---

## Two kinds of events

Before you touch any code, it helps to be precise about what "event" means in PyFly, because the framework uses the word for two distinct things.

**Application events** (`pyfly.context.events`) are framework lifecycle notifications: `ContextRefreshedEvent` fires when the dependency-injection container has finished wiring all beans; `ApplicationReadyEvent` fires when the HTTP server is accepting connections; `ContextClosedEvent` fires during shutdown. These events are dispatched by the `ApplicationEventBus`, which matches subscribers by Python class type rather than string pattern. They are framework plumbing — useful for starting background workers or seeding caches, but not for business logic.

**Domain events** (`pyfly.eda`) are business-level facts: *a wallet was opened*, *funds were deposited*, *a transfer was completed*. These events are wrapped in an `EventEnvelope` that pairs the payload with rich metadata and published through the `EventPublisher` port. Subscriptions are matched by pattern strings such as `"wallet.opened"` or `"wallet.*"`. They are the subject of this chapter.

The distinction matters because the bus you choose changes what you can do with the events. The `ApplicationEventBus` dispatches to Python callables keyed by class; `InMemoryEventBus` routes by glob pattern and can be swapped for a Kafka-backed adapter without touching subscriber code. For all business reactions in Lumen, use domain events.

!!! note "Application events are still useful"
    If you need to warm a cache as soon as the application is ready, `@app_event_listener` on `ApplicationReadyEvent` is the right tool. The two systems coexist; you can use both in the same service.

---

## Publishing events

### The EventPublisher port

The publisher side of the domain event system is the `EventPublisher` protocol — a port in the hexagonal sense. Any code that needs to publish an event depends on this interface, not on any specific bus implementation. The protocol exposes two methods:

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

`publish` wraps your data in an `EventEnvelope` before delivery; you never construct the envelope manually when calling the publisher. `subscribe` is how you register handlers — though in practice you will use the `@event_listener` decorator rather than calling `subscribe` directly.

### The EventEnvelope

Every domain event reaches its listeners wrapped in an `EventEnvelope`. The envelope is a frozen dataclass — immutable once created — that pairs the payload with metadata you can rely on in every handler.

| Field | Type | Default | Description |
|---|---|---|---|
| `event_type` | `str` | required | Dot-separated identifier, e.g. `"wallet.opened"`. Used for pattern matching. |
| `payload` | `dict[str, Any]` | required | The event data. |
| `destination` | `str` | required | Logical channel or topic. |
| `event_id` | `str` | auto UUID | Unique ID for this event instance. |
| `timestamp` | `datetime` | `datetime.now(UTC)` | UTC creation time. |
| `headers` | `dict[str, str]` | `{}` | Arbitrary metadata: correlation IDs, trace context, and so on. |

Because the envelope is frozen, handlers can safely pass it between coroutines without defensive copying. The `event_id` is generated by the bus, so idempotency keys are always available without any effort from the publisher.

### Wiring the publisher into the command handlers

In Chapter 7 the command handlers loaded aggregates, drove domain behaviour, and saved — leaving the buffered events on the floor. Now you inject an `EventPublisher` and drain those events yourself after a successful save.

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

The pattern is deliberate: save first, publish second. If the save fails, no event is published — the listeners never see a fact that did not actually persist. If the publish fails after a successful save you have an at-least-once delivery challenge, which Chapter 10 will address with transactional outbox patterns. For now, the in-memory bus does not fail.

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

When `credit_wallet` returns, the bus receives the dict as the event payload automatically. `@publish_result` also accepts an optional `condition` predicate so the event is only published when the result satisfies a test — useful for conditional workflows where not every successful execution should broadcast.

::: figure art/figures/08-eda.svg | Figure 8.1 — One publisher, many independent listeners.

!!! spring "Spring parity"
    `EventPublisher` is PyFly's counterpart of Spring's `ApplicationEventPublisher`. Calling `publisher.publish(...)` is equivalent to `applicationEventPublisher.publishEvent(event)`. The `@event_listener` decorator (next section) mirrors Spring's `@EventListener` for synchronous, same-transaction reactions and `@TransactionalEventListener` (with `phase = AFTER_COMMIT`) for reactions that should run only after the write is committed. `@publish_result` has no direct Spring equivalent but achieves what Spring developers often wire manually with AOP `@AfterReturning` advice.

---

## Reacting with @event_listener

Publishing an event is only half the work. The value comes from what subscribes to it. PyFly's `@event_listener` decorator is the simplest way to register a listener: decorate any async function with the patterns it cares about, and the `ApplicationContext` wires the subscription during startup — no bus reference required at decoration time.

```python
from pyfly.eda import event_listener, EventEnvelope

@event_listener(["wallet.opened"])
async def on_wallet_opened(envelope: EventEnvelope) -> None:
    ...
```

The pattern list supports globs: `["wallet.*"]` matches every event whose type starts with `"wallet."`.

### BalanceProjection

The query handlers in Chapter 7 loaded the full aggregate on every `GetBalance` call, even when nothing had changed. A *projection* maintains a separate, denormalised read model that stays in sync through events — so the query handler reads a single row rather than reconstructing the aggregate from scratch.

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

The listener receives an `EventEnvelope` from the bus. It reads the payload fields that were put there by `wallet.deposit` — `wallet_id`, `new_balance`, `currency` — and upserts the read-model row. Notice what is absent: no import of the `Wallet` aggregate, no call to any repository, no knowledge of how the deposit was processed. The projection reacts purely to the published fact.

!!! tip "Envelope metadata in projections"
    The `envelope.timestamp` gives you the authoritative event time — when the fact was recorded, not when the listener ran. Store it in your read model and you get a cheap `updated_at` column for free, with no clock skew between writer and reader.

### Notifier

The welcome notification for a new wallet owner should fire once, right after the wallet is opened, and it should know nothing about the HTTP request that triggered the opening. `@event_listener(["wallet.walletopened"])` is exactly the right hook:

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

### AuditLog

Compliance requires an immutable record of every financial movement. The `AuditLog` catches every wallet event with a wildcard pattern:

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

The `wallet.*` pattern matches `wallet.walletopened`, `wallet.fundsdeposited`, and `wallet.fundswithdrawn` in one subscription. New event types whose names start with `wallet.` are captured automatically — the `AuditLog` never needs editing.

What makes the design compelling is that adding these three listeners required zero changes to the command handlers, the `Wallet` aggregate, or the repositories. The `DepositFundsHandler` from Listing 8.1 has no idea that a projection exists. The `Notifier` has no idea that a handler exists. They are entirely independent — each is a consequence of the same published fact, wired together only by the bus.

---

## When listeners fail: error strategies

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

!!! warning "Side effects and idempotency"
    If a listener performs a side effect — writing a database row, sending an email — and the bus retries delivery after a transient failure, the effect can run more than once. Design listeners to be idempotent: write a row only if the `event_id` has not already been recorded, send an email only if the welcome flag is not already set. The `envelope.event_id` (a stable UUID generated by the bus) is your idempotency key.

!!! tip "Match strategy to listener criticality"
    The `AuditLog` should use `LOG_AND_CONTINUE` — a broken audit logger must not halt a financial transaction. The `BalanceProjection`, which drives query responses, might warrant `RETRY` to ensure the read model stays consistent. The `Notifier` can tolerate `IGNORE` since a missed welcome email is not a data integrity issue.

---

## In-memory today, a broker tomorrow

`InMemoryEventBus` is the out-of-the-box implementation and the default that the `ApplicationContext` provides when you inject `EventPublisher`. It runs entirely in-process: `publish` is a direct async function call, there is no serialization, and if the process dies any un-delivered events are lost.

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

When you call `publish`, the bus:

1. Wraps the arguments in an `EventEnvelope` with a generated `event_id` and a UTC `timestamp`.
2. Iterates every registered `(pattern, handler)` pair.
3. For each pair where `fnmatch.fnmatch(event_type, pattern)` is `True`, invokes the handler with the envelope.
4. Handlers are called sequentially in subscription order.

Subscriptions use Python's `fnmatch` under the hood, so `"wallet.*"` matches `"wallet.opened"` but not `"wallet.opened.extra"`, and `"*"` matches everything.

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
