<span class="eyebrow">Chapter 8</span>

# Domain Events & Event-Driven Architecture {.chtitle}

::: figure art/openers/ch08.svg | &nbsp;

Lumen's wallet saves correctly, validates rigorously, and dispatches every write through a typed command. But look at what the command handlers do after `repo.add`: nothing. The domain events that the `Wallet` aggregate buffers — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are drained and discarded. The bus pipeline that Chapter 7 promised would "publish domain events" has nowhere to send them yet.

The gap matters in practice. Lumen needs a balance read model that stays in sync without reloading the aggregate on every query, a welcome notification when a new wallet opens, and an immutable audit trail of every financial movement for compliance. All three features depend on knowing *that something happened* — not who requested it or how it was handled.

**Domain events** are the answer. Instead of having a command handler call the notification service directly — or coupling the audit log to the repository — you publish the event: a concise, timestamped, immutable record of a fact. Every interested party subscribes independently. The handler that saved the wallet does not need to know what the auditor does, or even that an auditor exists. You can add a new subscriber months later without touching a single line of existing handler code.

This chapter builds the reaction side of Lumen's architecture. You will wire `EventPublisher` into the command handlers, introduce the `publish_domain_events` bridge that drains the aggregate's buffer and forwards each event to the bus, and write a `WalletAuditListener` that subscribes using `@event_listener` and maintains two in-memory projections: an immutable audit trail and a running deposit total. By the end of the chapter the write path and the read infrastructure are fully decoupled — each side evolves without the other noticing.

---

## Two kinds of events

Before touching any code, it is worth being precise about what "event" means in PyFly. The framework uses the word for two distinct things, and confusing them leads to the wrong bus, the wrong subscription API, and subtle runtime surprises.

**Application events** (`pyfly.context.events`) are framework lifecycle notifications. `ContextRefreshedEvent` fires when the DI container finishes wiring; `ApplicationReadyEvent` fires when the HTTP server begins accepting connections; `ContextClosedEvent` fires during shutdown. The `ApplicationEventBus` dispatches them to subscribers matched by Python class type — they are infrastructure plumbing for bootstrapping, deliberately separate from any business concept.

**Domain events** (`pyfly.eda`) are business-level facts: *a wallet was opened*, *funds were deposited*, *a transfer was completed*. The `EventPublisher` port wraps each payload in an `EventEnvelope` and routes it by the domain event class name — `"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"` — so listeners subscribe to named business facts rather than implementation details. Domain events are the subject of this chapter.

The distinction shapes what you can do with each kind. The `ApplicationEventBus` dispatches to callables keyed by class; `InMemoryEventBus` routes by class name and can be swapped for a Kafka-backed adapter without touching subscriber code. The rule is simple: use lifecycle events for infrastructure bootstrapping, domain events for everything with business meaning.

!!! note "Application events are still useful"
    If you need to warm a cache as soon as the application is ready, `@app_event_listener` on `ApplicationReadyEvent` is the right tool. The two systems coexist; you can use both in the same service.

---

## Publishing events

### The EventPublisher port

The first question a new team member usually asks is: "which class do I import to fire an event?" The answer is deliberately not a class — it is a protocol. `EventPublisher` is a **port** in the hexagonal-architecture sense: any code that needs to publish an event depends on this interface, and the bus implementation is injected from outside. That design decision is what lets you run `InMemoryEventBus` locally today and swap in a Kafka adapter in production without touching a single handler.

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

`publish` wraps your data in an `EventEnvelope` before delivery — you never construct the envelope yourself. `subscribe` registers handlers programmatically, though in practice you will use the `@event_listener` decorator instead, because it lets the `ApplicationContext` wire subscriptions automatically at startup.

The bus bean exists only when `pyfly.eda.provider` is configured. For Lumen, `pyfly.yaml` sets it to `memory`:

::: listing pyfly.yaml | Listing 8.0 — Enabling the in-memory EDA bus in pyfly.yaml
pyfly:
  eda:
    provider: memory
  # … other keys omitted for brevity
:::

Without this line the `EventPublisher` bean is not registered and any handler that declares `events: EventPublisher` in its constructor will fail to start.

### The EventEnvelope

Every domain event reaches its listeners wrapped in an **`EventEnvelope`**. Think of it as the metadata layer that transforms a bare Python dictionary into a traceable, auditable, first-class fact. It is a frozen dataclass — immutable once created — that pairs the payload with the context every listener needs.

| Field | Type | Default | Description |
|---|---|---|---|
| `event_type` | `str` | required | The domain event class name, e.g. `"FundsDeposited"`. Used for routing. |
| `payload` | `dict[str, Any]` | required | The event data. |
| `destination` | `str` | required | Logical channel or topic, e.g. `"wallet.events"`. |
| `event_id` | `str` | auto UUID | Unique ID for this event instance. |
| `timestamp` | `datetime` | `datetime.now(UTC)` | UTC creation time. |
| `headers` | `dict[str, str]` | `{}` | Arbitrary metadata: correlation IDs, trace context, and so on. |

Three fields deserve particular attention. `event_id` is a stable UUID generated by the bus at publish time — your **idempotency key** for exactly-once semantics, available in every listener with no extra work. `timestamp` records when the fact was observed, not when the listener processes it, so it stays accurate even if a listener runs with a delay. `headers` carries cross-cutting concerns such as distributed trace IDs — metadata that has nothing to do with the business payload but matters enormously for observability. Because the envelope is frozen, handlers can safely pass it across async boundaries without defensive copying.

`event_type` holds the **class name** of the domain event — `"WalletOpened"`, `"FundsDeposited"`, or `"FundsWithdrawn"` — not a dot-separated path. Listeners subscribe by those same class names, so the subscription contract is defined by the domain model, not by string conventions invented outside it.

### The domain events in the Wallet aggregate

The `Wallet` aggregate raises typed, frozen-dataclass domain events. Each event's class name becomes its routing `event_type` on the bus:

::: listing lumen/models/entities/v1/wallet_entity.py | Listing 8.1 — Domain events raised by the Wallet aggregate
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent


@dataclass(frozen=True)
class WalletOpened(DomainEvent):
    wallet_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass(frozen=True)
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    def deposit(self, amount: Money) -> None:
        """Credit *amount*; raises FundsDeposited."""
        self._assert_currency(amount)
        self.balance = self.balance.add(amount)
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )
    # … open() and withdraw() follow the same pattern
:::

`DomainEvent` is a base frozen dataclass. Its `event_type` property returns `type(self).__name__` — the class name — which is exactly what `EventPublisher` uses as the routing key. `raise_event` buffers the event in the aggregate; the command handler drains that buffer by calling `wallet.clear_events()` after a successful persist.

### The publish bridge

Rather than repeating the drain loop in every command handler, Lumen extracts it into a single `publish_domain_events` coroutine. The bridge serialises each drained event with `dataclasses.asdict`, then calls `publisher.publish` with the class name as `event_type` and `"wallet.events"` as the logical channel:

::: listing lumen/core/services/wallets/event_publishing.py | Listing 8.2 — publish_domain_events bridges drained events to the EDA bus
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from lumen.core.services.listeners.wallet_audit_listener import (
    WALLET_EVENTS_DESTINATION,
)
from pyfly.domain import DomainEvent
from pyfly.eda import EventPublisher


def _to_payload(event: DomainEvent) -> dict[str, Any]:
    """Flatten a frozen-dataclass domain event into a dict."""
    payload: dict[str, Any] = dataclasses.asdict(event)
    payload.setdefault("event_type", event.event_type)
    return payload


async def publish_domain_events(
    publisher: EventPublisher, events: Iterable[DomainEvent]
) -> None:
    """Publish each drained domain event on the wallet events channel.

    The envelope's ``event_type`` is the domain event class name
    (``WalletOpened`` / ``FundsDeposited`` / ``FundsWithdrawn``).
    """
    for event in events:
        await publisher.publish(
            destination=WALLET_EVENTS_DESTINATION,
            event_type=event.event_type,
            payload=_to_payload(event),
        )
:::

`WALLET_EVENTS_DESTINATION` is the constant `"wallet.events"` defined in `wallet_audit_listener.py` and shared by publisher and listener so the channel name cannot drift. `event.event_type` is the class-name property on `DomainEvent`: `"WalletOpened"`, `"FundsDeposited"`, or `"FundsWithdrawn"`.

### Wiring the publisher into the command handlers

In Chapter 7 the command handlers loaded aggregates, drove domain behaviour, and saved — leaving buffered events on the floor. Now you close that gap. Inject an `EventPublisher` alongside the repository, and after a successful save drain the aggregate's buffer and publish each event through the bridge.

Here is the updated `DepositFundsHandler`:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listing 8.3 — DepositFundsHandler drains and publishes the aggregate's buffered events
from __future__ import annotations

from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.models.entities.v1.money import Money
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.domain import AggregateNotFound
from pyfly.eda import EventPublisher


@command_handler
@service
class DepositFundsHandler(CommandHandler[DepositFunds, int]):
    """Credit funds to an existing wallet; returns the new balance."""

    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: DepositFunds) -> int:
        wallet = await self._repository.find(command.wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", command.wallet_id)

        wallet.deposit(Money(amount=command.amount, currency=wallet.currency))
        await self._repository.add(wallet)

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet.balance.amount
:::

Two design decisions are worth noting. First, `events: EventPublisher` is typed as the protocol, not as `InMemoryEventBus`. The DI container injects whichever implementation is registered — the handler never knows or cares which one. Second, the publish call sits *after* `self._repository.add(wallet)`. That ordering is intentional: if the save fails, no event is published, so listeners never see a fact that never persisted. If the publish fails after a successful save you have an at-least-once delivery challenge — Chapter 10 addresses that with transactional outbox patterns. For now, the in-memory bus never fails.

The `OpenWalletHandler` follows the same pattern:

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listing 8.4 — OpenWalletHandler publishes WalletOpened after saving
from __future__ import annotations

from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events

    async def do_handle(self, command: OpenWallet) -> str:
        wallet_id = await self._repository.next_id()
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.add(wallet)

        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

`return wallet_id` comes *after* the publish call — the handler fulfils its contract only once every fact produced by the operation has been dispatched.

### The @publish_result shortcut

In simpler services where a method's return value *is* the event payload — common in code that has not adopted the full aggregate pattern — `@publish_result` removes the manual publish call entirely:

::: listing lumen/eda/publish_result_example.py | Listing 8.5 — @publish_result auto-publishes the method's return value
from pyfly.eda import publish_result
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()


@publish_result(bus, destination="wallet.events", event_type="FundsTransferred")
async def transfer_funds(source_id: str, target_id: str, amount: int) -> dict:
    # Business logic omitted — the returned dict IS the event payload.
    return {
        "source_id": source_id,
        "target_id": target_id,
        "amount": amount,
    }
:::

When `transfer_funds` returns, the decorator intercepts the result and calls `bus.publish` with it as the payload — no boilerplate loop needed. `destination` and `event_type` are fixed at decoration time, keeping the business function clean. `@publish_result` also accepts an optional `condition` predicate: the event is published only when the result satisfies the test, which is useful for conditional workflows where not every successful execution should broadcast.

::: figure art/figures/08-eda.svg | Figure 8.1 — One publisher, many independent listeners.

!!! spring "Spring parity"
    `EventPublisher` is PyFly's counterpart of Spring's `ApplicationEventPublisher`. Calling `publisher.publish(...)` is equivalent to `applicationEventPublisher.publishEvent(event)`. The `@event_listener` decorator (next section) mirrors Spring's `@EventListener` for synchronous, same-transaction reactions. `@publish_result` achieves what Spring developers often wire manually with AOP `@AfterReturning` advice.

---

## Reacting with @event_listener

Publishing an event is only half the picture. An event that nobody reacts to is just a log entry. The value of the event-driven model lies in the *reactions* it enables — independent behaviours that activate in response to the same published fact, each unaware of the others.

PyFly's **`@event_listener`** decorator is the simplest way to register a reaction. Decorate any async method with the class names it cares about, and `ApplicationContext` wires the subscription during startup — no bus reference needed at decoration time.

```python
from pyfly.eda import event_listener, EventEnvelope

@event_listener(event_types=["FundsDeposited"])
async def on_funds_deposited(envelope: EventEnvelope) -> None:
    ...
```

`event_types` accepts exact class names. Listeners inside a `@service` class receive an `EventEnvelope` as their sole argument. Because matching happens at the bus level — not inside your function — a single listener method can subscribe to multiple event types in one declaration.

### WalletAuditListener

Lumen's production listener is `WalletAuditListener`. It subscribes to all three wallet domain events and maintains two in-memory projections: an ordered **audit trail** and a **running net-deposit total** per wallet.

::: listing lumen/core/services/listeners/wallet_audit_listener.py | Listing 8.6 — WalletAuditListener: audit trail + running-total projection
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener

logger = logging.getLogger(__name__)

WALLET_EVENTS_DESTINATION = "wallet.events"


@dataclass(frozen=True)
class AuditEntry:
    """One observed domain event, captured for the audit trail."""

    event_type: str
    wallet_id: str
    event_id: str
    occurred_at: datetime
    payload: dict[str, object]


@service
class WalletAuditListener:
    """In-memory audit log + running-total projection over wallet events."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._running_totals: dict[str, int] = {}

    @event_listener(
        event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"]
    )
    async def on_wallet_event(self, envelope: EventEnvelope) -> None:
        """Project every wallet domain event into the read models."""
        payload = dict(envelope.payload)
        wallet_id = str(payload.get("wallet_id", ""))

        self._entries.append(
            AuditEntry(
                event_type=envelope.event_type,
                wallet_id=wallet_id,
                event_id=str(payload.get("event_id", envelope.event_id)),
                occurred_at=envelope.timestamp,
                payload=payload,
            )
        )

        if envelope.event_type == "WalletOpened":
            self._running_totals.setdefault(wallet_id, 0)
        elif envelope.event_type == "FundsDeposited":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = (
                self._running_totals.get(wallet_id, 0) + amount
            )
        elif envelope.event_type == "FundsWithdrawn":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = (
                self._running_totals.get(wallet_id, 0) - amount
            )

        logger.info(
            "wallet_audit_observed",
            extra={"event_type": envelope.event_type, "wallet_id": wallet_id},
        )

    @property
    def entries(self) -> list[AuditEntry]:
        """A snapshot of the audit log, in observation order."""
        return list(self._entries)

    def entries_for(self, wallet_id: str) -> list[AuditEntry]:
        """The audit entries recorded for one wallet."""
        return [e for e in self._entries if e.wallet_id == wallet_id]

    def running_total(self, wallet_id: str) -> int:
        """Net funds (deposited minus withdrawn) for wallet_id, minor units."""
        return self._running_totals.get(wallet_id, 0)
:::

Here is what the listener does, step by step.

`@event_listener(event_types=["WalletOpened", "FundsDeposited", "FundsWithdrawn"])` tells `ApplicationContext` to subscribe `on_wallet_event` to those three class names. Because the class is a `@service` bean, PyFly discovers it at startup and wires the subscriptions automatically — you never call `bus.subscribe` by hand.

`on_wallet_event` receives an `EventEnvelope`. `envelope.event_type` is the class name of the raised domain event. `envelope.payload` is the dict produced by `dataclasses.asdict` in the publish bridge, so its keys match the dataclass field names exactly — `wallet_id`, `amount`, `currency`, `balance`.

The method appends an `AuditEntry` for every event, then branches on `event_type` to update the running total. Notice what is absent: no import of the `Wallet` aggregate, no repository call, no knowledge of how the deposit was processed. The projection reacts purely to the published fact.

!!! tip "Envelope metadata in projections"
    `envelope.timestamp` gives you the authoritative event time — when the fact was recorded, not when the listener ran. Store it in your read model and you get a cheap `occurred_at` column for free, with no clock skew between writer and reader.

### Testing the listener end-to-end

The test suite exercises the full publish-and-receive path with no mocks. The conftest wires a shared `InMemoryEventBus`, mirrors the `@event_listener` discovery step by subscribing `on_wallet_event` to each declared class name, and registers real command handlers that share the same bus reference:

```python
# tests/conftest.py (abbreviated)
from pyfly.eda.adapters.memory import InMemoryEventBus

@pytest_asyncio.fixture
async def event_bus() -> InMemoryEventBus:
    yield InMemoryEventBus()

@pytest_asyncio.fixture
async def audit_listener(event_bus: InMemoryEventBus) -> WalletAuditListener:
    listener = WalletAuditListener()
    method = listener.on_wallet_event
    for pattern in method.__pyfly_event_patterns__:
        event_bus.subscribe(pattern, method)
    yield listener
```

With that wiring in place, the test sends real commands and asserts on the listener's read models:

::: listing lumen/tests/test_event_listener.py | Listing 8.7 — End-to-end test: commands publish, listener projects
from __future__ import annotations

import pytest
from lumen.core.services.listeners import WalletAuditListener
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus


@pytest.mark.asyncio
async def test_listener_observes_wallet_events(
    command_bus: DefaultCommandBus,
    audit_listener: WalletAuditListener,
) -> None:
    wallet_id = await command_bus.send(
        OpenWallet(owner_id="u-1", currency=Currency.EUR)
    )
    await command_bus.send(DepositFunds(wallet_id=wallet_id, amount=1500))
    await command_bus.send(WithdrawFunds(wallet_id=wallet_id, amount=400))

    entries = audit_listener.entries_for(wallet_id)
    assert [e.event_type for e in entries] == [
        "WalletOpened",
        "FundsDeposited",
        "FundsWithdrawn",
    ]

    deposited = entries[1]
    assert deposited.payload["amount"] == 1500
    assert deposited.payload["currency"] == "EUR"
    assert deposited.payload["balance"] == 1500

    # running_total = deposited − withdrawn
    assert audit_listener.running_total(wallet_id) == 1100
:::

The test proves the full chain: `OpenWalletHandler` → `publish_domain_events` → `InMemoryEventBus` → `WalletAuditListener.on_wallet_event` → `audit_listener.entries_for(...)`. No mocks, no fakes — the production code path runs as written.

What makes this design compelling is that adding the listener required zero changes to the command handlers, the `Wallet` aggregate, or any repository. `DepositFundsHandler` has no idea a projection exists. Both sides are entirely independent — each is a consequence of the same published fact, connected only by the bus.

---

## When listeners fail: error strategies

A misbehaving listener raises a pointed question: should the failure stop the entire delivery chain, or should the bus continue notifying the remaining listeners? The right answer depends on the listener's role. PyFly gives you explicit control rather than imposing a single policy.

By default, `InMemoryEventBus` invokes listeners sequentially and propagates any exception — the right behaviour for development, where a failing listener should surface loudly. In production you usually need finer control.

**`ErrorStrategy`** is an enum that governs how the bus behaves when a listener raises:

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

!!! tip "Match strategy to listener criticality"
    An audit listener should use `LOG_AND_CONTINUE` — a broken audit logger must not halt a financial transaction. A projection that drives query responses might warrant `RETRY` to ensure the read model stays consistent. A notifier can tolerate `IGNORE` since a missed welcome email is not a data integrity issue.

!!! warning "Side effects and idempotency"
    If a listener performs a side effect — writing a database row, sending an email — and the bus retries delivery after a transient failure, the effect can run more than once. Design listeners to be idempotent: write a row only if the `event_id` has not already been recorded, send an email only if the welcome flag is not already set. The `envelope.event_id` (a stable UUID generated by the bus) is your idempotency key.

---

## In-memory today, a broker tomorrow

**`InMemoryEventBus`** is the out-of-the-box implementation — the default `EventPublisher` the `ApplicationContext` provides. It runs entirely in-process: `publish` is a direct async call, there is no serialisation, and undelivered events vanish if the process dies. For local development, integration tests, and monoliths that need no cross-process delivery that is perfectly acceptable.

Understanding how the in-memory bus works internally makes it easier to reason about behaviour at the edges — and to appreciate exactly what changes when you swap in a broker.

```python
from pyfly.eda.adapters.memory import InMemoryEventBus

bus = InMemoryEventBus()

bus.subscribe("FundsDeposited", my_handler)

await bus.publish(
    destination="wallet.events",
    event_type="FundsDeposited",
    payload={"wallet_id": "w-001", "amount": 5000, "currency": "EUR", "balance": 5000},
)
```

A `publish` call executes four steps in sequence:

1. Wraps the arguments in an `EventEnvelope` with a generated `event_id` and a UTC `timestamp`.
2. Iterates every registered `(pattern, handler)` pair.
3. For each pair where `fnmatch.fnmatch(event_type, pattern)` is `True`, calls the handler with the envelope.
4. Handlers run sequentially in subscription order.

Subscriptions use Python's `fnmatch`, so `"Funds*"` matches both `"FundsDeposited"` and `"FundsWithdrawn"`, and `"*"` matches everything. The sequential invocation in step 4 makes listener order deterministic — useful in tests — but it also means a slow listener delays all subsequent ones. Broker-backed adapters typically dispatch in parallel; keep that difference in mind when reasoning about throughput.

Because every listener in Lumen depends on the `EventPublisher` *protocol*, not on `InMemoryEventBus` directly, the implementation swaps without touching a single listener. Chapter 10 introduces Kafka and RabbitMQ adapters; switching either in is a configuration change — `WalletAuditListener` keeps working without modification.

!!! note "InMemoryEventBus and testing"
    `InMemoryEventBus` is also the right tool for tests. Inject a fresh `InMemoryEventBus` as a fixture, subscribe a capturing handler, exercise your command handler, and assert on the `EventEnvelope` objects the handler received — including `event_type`, `payload`, `event_id`, and `timestamp`. No mocking, no fakes, just the real bus with controlled inputs.

---

## What you built {.recap}

Part III is open.

This chapter closed the loop Chapter 7 began. `Wallet` raised domain events in Chapter 6; the command handlers published them here; `WalletAuditListener` reacts to those facts without knowing anything about the command path that triggered them.

The architecture is genuinely event-driven within a single process. Here is a quick reference to each piece:

| Piece | Role |
|---|---|
| `EventPublisher` | Port — a protocol any bus implementation fulfils |
| `InMemoryEventBus` | Default adapter — in-process, zero config; activated by `pyfly.eda.provider: memory` |
| `EventEnvelope` | Carries payload + `event_id`, `timestamp`, `destination`, `headers` |
| `@event_listener(event_types=[...])` | Subscription decorator — class names; context wires it at startup |
| `publish_domain_events` | Bridge — drains `wallet.clear_events()`, serialises with `dataclasses.asdict`, calls `publisher.publish` |
| `ErrorStrategy` | Controls failure handling: `IGNORE`, `LOG_AND_CONTINUE`, `RETRY`, `DEAD_LETTER`, `FAIL_FAST` |

Three principles carry forward into the rest of Part III: **save before you publish** — listeners must never see uncommitted facts; **design listeners for idempotency** — retries must be safe; **depend on the port, not the adapter** — the bus can be swapped without touching listener code.

Chapter 9 pushes the event idea further. Instead of maintaining a separate read model alongside a mutable aggregate, you store the events themselves as the system of record — event sourcing the ledger so that every historical balance is computable from first principles.

---

## Try it yourself {.exercises}

1. **Add a `FraudDetector` listener.** Create a `FraudDetector` service that subscribes to `"FundsDeposited"` using `@event_listener(event_types=["FundsDeposited"])`. If the `amount` in the payload exceeds `1_000_000` (ten thousand euros in minor units), log a warning that includes the `envelope.event_id`, `envelope.timestamp`, and the `wallet_id` from the payload. Verify it fires by publishing a `FundsDeposited` event directly to an `InMemoryEventBus` in a unit test, and assert that the warning was triggered.

2. **Extend `WalletAuditListener` with per-event filtering.** Add a method `entries_by_type(self, event_type: str) -> list[AuditEntry]` that returns only the entries with a matching `event_type`. Write a test that opens a wallet, makes two deposits and one withdrawal, and asserts that `entries_by_type("FundsDeposited")` returns exactly two entries.

3. **Observe error strategy behaviour.** Create a listener whose handler raises `RuntimeError("failure")` unconditionally. Register it alongside a list-appending capturing handler on an `InMemoryEventBus`. Configure `ErrorStrategy.LOG_AND_CONTINUE` and confirm the capturing handler still receives the event despite the failure. Then switch to `ErrorStrategy.FAIL_FAST` and confirm the capturing handler does *not* receive the event.
