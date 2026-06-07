<span class="eyebrow">Chapter 9</span>

# Event Sourcing the Ledger {.chtitle}

::: figure art/openers/ch09.svg | &nbsp;

Chapter 8 ended with a gap. The `BalanceProjection` listener keeps a fast read model by reacting to `wallet.fundsdeposited` and `wallet.fundswithdrawn` events — but the canonical state of the wallet is still a row in the `wallets` table. That row holds a single `balance` column. Every time the balance changes, the old value is gone forever. If an auditor asks "what was the balance of wallet `w-001` at 14:32 on the 3rd of March?", the answer is: you cannot know. The database remembers only the present.

Event sourcing turns this design inside out. Instead of storing the *current* state and discarding each change, you store the *sequence of changes* and derive the current state by replaying them. The `wallets` table disappears. In its place is an **event stream**: an append-only log of every `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` event the wallet has ever produced. The balance at any point in time is a pure function of the events that occurred up to that moment. You can rewind to 14:32 on any date in history because you have everything that happened between then and now.

A financial ledger is the ideal domain for event sourcing. Accountants have understood for centuries that a ledger's authority comes from its entries, not from a running total scratched at the bottom of a column. The running total is a *derived fact*; the entries are the *source of truth*. PyFly's `pyfly.eventsourcing` module brings that accounting intuition into code: aggregates emit domain events, an `EventStore` records them immutably, a repository replays the stream to reconstruct state, and a `ProjectionRunner` builds read models on top.

This chapter rebuilds the `Wallet` aggregate as a fully event-sourced aggregate, introduces every component of the `pyfly.eventsourcing` module, and shows you how the event store, snapshotting, projections, and the transactional outbox work together to give Lumen a ledger that is both auditable and performant.

---

## From state to events

The clearest way to understand the shift is to compare what the database looks like in each model.

In the **state-storage model** the database holds the current state of the wallet:

| wallet_id | owner_id | balance_cents | currency | updated_at |
|---|---|---|---|---|
| w-001 | u-42 | 8500 | EUR | 2026-03-03 17:11 |

Every `deposit` and `withdraw` operation overwrites `balance_cents`. The history is gone. You know the wallet has 85.00 EUR right now; you cannot know how it got there.

In the **event-storage model** the database holds the event stream:

| stream_id | seq | event_type | payload | occurred_at |
|---|---|---|---|---|
| w-001 | 1 | WalletOpened | `{"currency":"EUR","owner_id":"u-42"}` | 2026-03-01 09:00 |
| w-001 | 2 | FundsDeposited | `{"amount":10000,"new_balance":10000}` | 2026-03-01 09:01 |
| w-001 | 3 | FundsWithdrawn | `{"amount":1500,"new_balance":8500}` | 2026-03-03 17:11 |

The current balance is still 85.00 EUR — but now you can read every decision that led to it. An auditor, a regulator, a fraud investigator can replay the stream from any offset and see exactly what happened and when.

::: figure art/figures/09-eventsourcing.svg | Figure 9.1 — State storage vs event storage: one model keeps a snapshot of the present; the other keeps every fact that led to it.

The trade-off is real. Event storage makes reads more expensive by default — you must replay the stream to learn the current balance — and it requires discipline around schema evolution (events are immutable; you cannot change a field name after the fact). Both concerns have solutions in PyFly: **snapshots** accelerate replay of long streams, and **upcasters** translate old event shapes to new ones during load. You will see both before the end of this chapter.

!!! note "Events as the system of record"
    Event sourcing is not the same as event-driven architecture. Chapter 8 used EDA: the aggregate stored its state normally and published domain events as a side effect. Event sourcing goes further: the events *are* the state. There is no separate `balance` column to keep in sync — the balance is computed by the repository every time it loads the aggregate.

---

## The event-sourced aggregate

In Chapter 6 the `Wallet` aggregate held `_balance: Money` as direct Python state. A `deposit` call added to it; a `withdraw` call subtracted from it. In the event-sourced version, the aggregate never mutates its own fields directly. Instead, every state change is mediated by a domain event: the behaviour method *applies* the event, the event handler *updates the fields*, and the `EventStore` persists the event. When the aggregate is loaded, the repository replays all historical events through the same handlers, rebuilding the in-memory state event by event.

This two-step indirection — apply then handle — is the core mechanic of event sourcing. It creates a strict discipline: every state transition is recorded exactly once, as an event, and the aggregate's current state is always provable from its history.

PyFly's `AggregateRoot` (imported from `pyfly.eventsourcing`) extends the domain-level `AggregateRoot` with this mechanic. It exposes:

- **`apply(event)`** — appends the event to the pending-events buffer *and* dispatches it immediately to the matching handler so the in-memory state updates without a round-trip through the store.
- **`when(EventType, handler_fn)`** — registers a handler for a given event class. The handler receives `(aggregate, event)` and performs the mutation.
- **`on_{event_type}` method convention** — an alternative to `when()`: if the aggregate defines a method named `on_fundsdeposited`, it is discovered and called automatically.
- **`version`** — an integer counter incremented after each successfully dispatched event. The store uses this as the optimistic-concurrency token.

The dispatch order is: registered `when()` handler first; then `on_{event_type}` method; if neither exists, `EventHandlerException` is raised — a missing handler would silently corrupt reconstructed state, so the aggregate fails loudly.

Here is the event-sourced `Wallet`:

::: listing lumen/eventsourcing/wallet_es.py | Listing 9.1 — WalletES: an event-sourced aggregate that derives its balance from replay
from __future__ import annotations

from dataclasses import dataclass

from pyfly.eventsourcing import AggregateRoot, DomainEvent

from lumen.domain.money import Money


# ── Domain events ─────────────────────────────────────────────────────────

@dataclass
class WalletOpened(DomainEvent):
    wallet_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass
class FundsDeposited(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    new_balance: int = 0


@dataclass
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    new_balance: int = 0


# ── Event-sourced aggregate ────────────────────────────────────────────────

class WalletES(AggregateRoot):
    """
    Event-sourced Wallet.  The balance is never stored directly —
    it is derived by replaying the event stream.
    """

    def __init__(self) -> None:
        super().__init__()
        self._owner_id: str = ""
        self._balance: Money = Money(0, "EUR")
        # Register event handlers with when()
        self.when(
            WalletOpened,
            lambda agg, evt: (
                setattr(agg, "id", evt.wallet_id),
                setattr(agg, "_owner_id", evt.owner_id),
                setattr(
                    agg,
                    "_balance",
                    Money(0, evt.currency),
                ),
            ),
        )
        self.when(
            FundsDeposited,
            lambda agg, evt: setattr(
                agg,
                "_balance",
                Money(evt.new_balance, agg._balance.currency),
            ),
        )
        self.when(
            FundsWithdrawn,
            lambda agg, evt: setattr(
                agg,
                "_balance",
                Money(evt.new_balance, agg._balance.currency),
            ),
        )

    # ── Read-only properties ───────────────────────────────────────────────

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def balance(self) -> Money:
        return self._balance

    # ── Factory ───────────────────────────────────────────────────────────

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: str) -> "WalletES":
        wallet = cls()
        wallet.id = wallet_id
        wallet.apply(WalletOpened(
            wallet_id=wallet_id,
            owner_id=owner_id,
            currency=currency,
        ))
        return wallet

    # ── Behaviour ─────────────────────────────────────────────────────────

    def deposit(self, amount: Money) -> None:
        if amount.currency != self._balance.currency:
            raise ValueError(
                f"Cannot deposit {amount.currency} "
                f"into a {self._balance.currency} wallet"
            )
        if amount.is_negative() or amount.is_zero():
            raise ValueError("Deposit amount must be positive")
        new_balance = self._balance.add(amount)
        assert self.id is not None
        self.apply(FundsDeposited(
            wallet_id=self.id,
            amount=amount.amount,
            currency=amount.currency,
            new_balance=new_balance.amount,
        ))

    def withdraw(self, amount: Money) -> None:
        if amount.currency != self._balance.currency:
            raise ValueError(
                f"Cannot withdraw {amount.currency} "
                f"from a {self._balance.currency} wallet"
            )
        new_balance = self._balance.subtract(amount)
        if new_balance.is_negative():
            raise ValueError("Insufficient funds")
        assert self.id is not None
        self.apply(FundsWithdrawn(
            wallet_id=self.id,
            amount=amount.amount,
            currency=amount.currency,
            new_balance=new_balance.amount,
        ))
:::

**How it works.** The `__init__` registers three `when()` handlers — one per event class — each of which performs only `setattr` mutations to the aggregate's fields. No arithmetic happens inside the handlers; the behaviour methods (`deposit`, `withdraw`) are responsible for all validation and for computing the new state before constructing the event. The handler simply *applies* the already-computed result.

`wallet.apply(FundsDeposited(...))` does two things atomically: it appends the event to the pending-events buffer (so the repository can persist it) and immediately dispatches it to the `FundsDeposited` handler (so `_balance` updates in memory). This means the aggregate's in-memory state is always consistent with its pending events, even before a save.

The factory method `open` calls `apply(WalletOpened(...))` rather than setting fields directly. This matters: if you ever loaded this aggregate from its event stream, the `WalletOpened` event would pass through exactly the same handler, and the result would be identical. State and replay are identical code paths — that symmetry is the correctness guarantee of event sourcing.

The `version` counter on `AggregateRoot` starts at zero and increments after each dispatched event. After `open`, `wallet.version == 1`. After one deposit, `wallet.version == 2`. You will see this number again when the `EventStore` enforces optimistic concurrency.

!!! tip "on_{event_type} as an alternative"
    Instead of `when()` lambdas, you can define methods on the aggregate class named after the event. `on_walletopened(self, evt: WalletOpened) -> None` is discovered automatically. Use `when()` for concise one-liners and explicit methods for handlers that need multiple statements or local variables.

!!! spring "Spring parity"
    `AggregateRoot` + `apply()` + `when()` is PyFly's equivalent of Axon Framework's `@Aggregate` + `AggregateLifecycle.apply(event)` + `@EventSourcingHandler`. Axon uses annotation-driven handler discovery (`@EventSourcingHandler`); PyFly uses `when()` registration or `on_*` method convention. The replaying mechanic — load events from the store, call the same handlers, rebuild state — is identical in both frameworks.

---

## The EventStore

The aggregate knows how to produce and replay events. The `EventStore` knows how to persist and retrieve them. These are deliberately separate concerns: the aggregate is pure business logic with no I/O; the event store is pure I/O with no business logic.

`EventStore` exposes two operations:

- **`append(aggregate_id, events, expected_version)`** — persists a batch of events for an aggregate stream. Raises a concurrency error if the stream's actual version does not match `expected_version`.
- **`load(aggregate_id)`** — returns the ordered sequence of event envelopes for an aggregate, from the first to the most recent.

`InMemoryEventStore` is the out-of-the-box implementation. Like `InMemoryEventBus` in Chapter 8, it runs entirely in-process with no I/O — perfect for development and tests. A production deployment would swap in a PostgreSQL- or EventStoreDB-backed adapter.

The `EventSourcedRepository` wraps the `EventStore` and handles the full save/load cycle. You never interact with the `EventStore` directly from application code; you call `repo.save(aggregate)` and `repo.load(aggregate_id)`, and the repository handles the rest.

Here is how you wire the repository and use it to save and reload a wallet:

::: listing lumen/eventsourcing/wallet_repository_es.py | Listing 9.2 — Saving and loading an event-sourced Wallet
from __future__ import annotations

from pyfly.eventsourcing import InMemoryEventStore, InMemorySnapshotStore
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.eventsourcing.wallet_es import WalletES
from lumen.domain.money import Money


async def demo_save_and_load() -> None:
    # Wire the repository with an in-memory store and snapshot store.
    store = InMemoryEventStore()
    snapshots = InMemorySnapshotStore()
    repo: EventSourcedRepository[WalletES] = EventSourcedRepository(
        store,
        factory=WalletES,
        snapshots=snapshots,
    )

    # Create and save a wallet with two operations.
    wallet = WalletES.open("w-001", "u-42", "EUR")
    wallet.deposit(Money(10000, "EUR"))   # +100.00 EUR
    wallet.withdraw(Money(1500, "EUR"))   # -15.00 EUR
    await repo.save(wallet)

    # Later — in a new request, new coroutine, new process — reload it.
    recovered = await repo.load("w-001")
    assert recovered is not None
    assert recovered.balance.amount == 8500   # 85.00 EUR
    assert recovered.version == 3             # opened + deposited + withdrawn
:::

**How it works.** `repo.save(wallet)` calls `store.append("w-001", pending_events, expected_version=0)`. The three events — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are serialized and written to the stream in order. The pending-events buffer on the aggregate is cleared.

`repo.load("w-001")` calls `store.load("w-001")`, receives the three event envelopes, constructs a fresh `WalletES()` via the `factory` argument, and replays each event through `apply()`. After replay, `recovered.balance.amount` is `8500` — the same value the live aggregate held after the three operations, computed without any shared state between the two objects.

The `EventSourcedRepository.load` method validates each replayed envelope:

- If `envelope.aggregate_id` does not match the requested ID, `EventHandlerException` is raised — this indicates a store bug or cross-aggregate data corruption.
- If `envelope.aggregate_type` is set and does not match the aggregate's class name, `EventHandlerException` is raised.

These checks are pessimistic by design. Silently replaying a corrupted stream would produce a wallet with the wrong balance — a financial error that could go undetected for weeks. Fail loudly; investigate immediately.

!!! note "Factory argument"
    `EventSourcedRepository` accepts a `factory` callable that produces a blank aggregate instance. Passing `factory=WalletES` is equivalent to `factory=lambda: WalletES()`. The factory must return an aggregate in its initial `__init__` state — no events applied — because the repository will apply the full history itself.

---

## Optimistic concurrency

Two concurrent requests — a deposit from the mobile app and an automated fee deduction from a background job — can load the same wallet at the same version, each apply their own change, and attempt to save. Without a concurrency guard, one of the saves silently wins and the other's events are lost. The resulting stream is internally inconsistent: the sequence numbers collide, the balance is wrong, and neither request received an error.

Optimistic concurrency prevents this. Before appending new events, the `EventStore` compares the stream's *current* version against the *expected* version the repository read at load time. If they match, the append proceeds and the stream version advances. If they do not match — because another writer already appended events — an error is raised and the losing request must retry from a fresh load.

The version comparison happens atomically inside the store's append operation. `InMemoryEventStore` uses a lock; a database-backed store uses a unique index on `(aggregate_id, sequence_number)` or a compare-and-swap.

!!! warning "Always handle concurrency errors"
    Optimistic concurrency raises an error when a conflict is detected. Your application service must catch it and decide what to do: retry the full load-mutate-save cycle (appropriate for low-contention writes), or surface a 409 Conflict to the caller (appropriate when the caller should re-submit with fresh data). Never silently swallow the error — a swallowed concurrency error leaves the stream in an inconsistent state.

The `expected_version` is passed implicitly by the repository: it remembers the version at which it loaded the aggregate and passes it to the store on save. You do not need to manage version numbers manually in application code. The concern is visible here only to help you reason about what happens under the hood:

::: listing lumen/eventsourcing/concurrency_demo.py | Listing 9.3 — Concurrent saves: one wins, one must retry
from __future__ import annotations

from pyfly.eventsourcing import InMemoryEventStore, InMemorySnapshotStore
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.eventsourcing.wallet_es import WalletES
from lumen.domain.money import Money


async def demo_concurrency() -> None:
    store = InMemoryEventStore()
    repo: EventSourcedRepository[WalletES] = EventSourcedRepository(
        store,
        factory=WalletES,
    )

    # Seed: open a wallet (version becomes 1 after save)
    wallet = WalletES.open("w-002", "u-99", "EUR")
    await repo.save(wallet)

    # Two coroutines each load the wallet at version 1.
    wallet_a = await repo.load("w-002")
    wallet_b = await repo.load("w-002")

    # Both apply their change — now each has one pending event.
    assert wallet_a is not None
    assert wallet_b is not None
    wallet_a.deposit(Money(5000, "EUR"))
    wallet_b.deposit(Money(3000, "EUR"))

    # First save wins: stream advances to version 2.
    await repo.save(wallet_a)

    # Second save fails: the stream is at version 2 but wallet_b
    # was loaded at version 1.  The EventStore raises an error.
    try:
        await repo.save(wallet_b)
        assert False, "Should have raised"
    except Exception as exc:
        # In production: catch, reload wallet, retry the deposit.
        print(f"Conflict detected — retry required: {exc}")
:::

**How it works.** `wallet_a` and `wallet_b` are loaded independently at version 1. After `wallet_a.deposit(...)`, its pending event list has one item. `repo.save(wallet_a)` appends that event with `expected_version=1`; the store sets the stream version to 2 and succeeds. When `repo.save(wallet_b)` runs next, the repository passes `expected_version=1` again — but the store's actual version is now 2. The store rejects the append and raises a concurrency error.

The retry pattern is deliberate: load the wallet again (now at version 2, which includes wallet_a's deposit), apply wallet_b's deposit on top, and save. After the retry the stream has both deposits in causal order, and the balance reflects both.

---

## Snapshots

Event sourcing trades write simplicity for read cost. Loading a wallet that has processed 10 000 transactions means replaying 10 000 events through the `apply()` chain every time the aggregate is needed. For most wallets the stream is short; for high-frequency accounts it can grow prohibitively long.

Snapshots address this. A snapshot is a serialized checkpoint of the aggregate's state captured at a specific version. When the repository loads an aggregate, it first looks for the most recent snapshot and, if found, deserializes the state directly to that version — then replays only the events that arrived after the snapshot. A snapshot at version 9 000 reduces a 10 000-event replay to 1 000 events.

PyFly's `InMemorySnapshotStore` stores snapshots in memory. You pass it to `EventSourcedRepository` alongside the event store:

```python
store = InMemoryEventStore()
snapshots = InMemorySnapshotStore()
repo = EventSourcedRepository(
    store,
    factory=WalletES,
    snapshots=snapshots,
)
```

The repository decides *when* to snapshot automatically using `snapshot_interval` (default `100`). After every `save`, it checks whether the aggregate's new version **crosses** a multiple of `snapshot_interval`:

```python
# crosses_interval is True when this batch pushes the stream past a
# 100-event boundary — e.g., version 95 → 105 crosses the 100 mark.
crossed = (
    (aggregate.version // snapshot_interval)
    > (previous_version // snapshot_interval)
)
```

This interval-crossing logic (rather than exact divisibility) handles the common case where a single save batch straddles the threshold — for example, a bulk import that adds 10 events takes the version from 95 to 105 and correctly triggers a snapshot even though neither 95 nor 105 is exactly divisible by 100.

Here is the snapshot lifecycle made explicit:

::: listing lumen/eventsourcing/snapshot_demo.py | Listing 9.4 — Snapshots: automatic checkpoint and fast reload
from __future__ import annotations

from pyfly.eventsourcing import InMemoryEventStore, InMemorySnapshotStore
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.eventsourcing.wallet_es import WalletES
from lumen.domain.money import Money


async def demo_snapshots() -> None:
    store = InMemoryEventStore()
    snapshots = InMemorySnapshotStore()
    repo: EventSourcedRepository[WalletES] = EventSourcedRepository(
        store,
        factory=WalletES,
        snapshots=snapshots,
        snapshot_interval=10,   # snapshot every 10 events for demo
    )

    wallet = WalletES.open("w-003", "u-77", "EUR")
    await repo.save(wallet)   # version 1 — no snapshot yet

    # Add 9 more events to cross the 10-event boundary.
    for _ in range(9):
        reloaded = await repo.load("w-003")
        assert reloaded is not None
        reloaded.deposit(Money(100, "EUR"))
        await repo.save(reloaded)
    # version is now 10 — the repository took a snapshot

    # The next load uses the snapshot + 0 new events.
    fast = await repo.load("w-003")
    assert fast is not None
    assert fast.version == 10
    assert fast.balance.amount == 900   # 9 deposits of 1.00 EUR
:::

**How it works.** After the tenth save the repository detects that the version crossed the 10-event boundary. It serializes the aggregate's current state into a snapshot envelope and stores it via `InMemorySnapshotStore`. The next `repo.load("w-003")` call first queries `snapshots.load("w-003")`, finds the checkpoint at version 10, deserializes it directly — skipping all 10 events — and then asks the event store for events with a sequence number greater than 10. There are none, so the reload cost is constant regardless of how many previous events exist.

!!! tip "Snapshot interval in production"
    A `snapshot_interval` of 100 is the default and a sensible starting point. For high-frequency wallets you might lower it; for accounts that only change a few times a day, a higher interval reduces snapshot-storage cost. Snapshots are an optimization, not a correctness requirement — removing them leaves the system correct but slower.

---

## Projections

The event store is the system of record. But most application queries — "what is the current balance?", "show all wallets for owner u-42", "which wallets are above 1 000 EUR?" — should not replay event streams on every read. They should hit a pre-computed read model: a table optimized for queries, kept in sync by a background process that consumes the event stream.

That background process is a **projection**. A projection subscribes to the event stream and updates a read model every time a relevant event arrives. PyFly provides `FunctionProjection` and `ProjectionRunner` in `pyfly.eventsourcing.projection`.

- **`FunctionProjection(name, handler_fn)`** wraps an async function that receives one `EventEnvelope` and updates the read model.
- **`ProjectionRunner(projection, store)`** drives the projection: it polls the `EventStore` for new events and calls the projection handler for each one.

Here is a `BalanceLedgerProjection` that builds a balance read model from the wallet's event stream:

::: listing lumen/eventsourcing/balance_projection.py | Listing 9.5 — BalanceLedgerProjection: a read model built from the event stream
from __future__ import annotations

from pyfly.eventsourcing import InMemoryEventStore
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner


# The in-process read model — in production, replace with a DB table.
_balance_store: dict[str, dict] = {}


async def _handle_envelope(envelope: object) -> None:
    """Update the balance read model for each wallet event."""
    # envelope is an EventEnvelope with .event_type and .payload
    ev = envelope  # type: ignore[assignment]
    event_type: str = getattr(ev, "event_type", "")
    payload: dict = getattr(ev, "payload", {})

    if event_type == "WalletOpened":
        _balance_store[payload["wallet_id"]] = {
            "wallet_id": payload["wallet_id"],
            "owner_id": payload.get("owner_id", ""),
            "balance_cents": 0,
            "currency": payload.get("currency", ""),
        }
    elif event_type in ("FundsDeposited", "FundsWithdrawn"):
        wallet_id = payload["wallet_id"]
        if wallet_id in _balance_store:
            _balance_store[wallet_id]["balance_cents"] = (
                payload["new_balance"]
            )


def build_projection(store: InMemoryEventStore) -> ProjectionRunner:
    projection = FunctionProjection("balance_ledger", _handle_envelope)
    return ProjectionRunner(projection, store)


async def demo_projection(store: InMemoryEventStore) -> None:
    runner = build_projection(store)
    await runner.start()

    # After start(), the runner has consumed all existing events.
    balance = _balance_store.get("w-001", {})
    print(f"Balance read model: {balance}")
:::

**How it works.** `FunctionProjection("balance_ledger", _handle_envelope)` wraps the async handler. `ProjectionRunner(projection, store)` links it to the `InMemoryEventStore`. Calling `await runner.start()` causes the runner to iterate every envelope in the store — in sequence-number order — and call `_handle_envelope` for each. After `start()` returns, `_balance_store` reflects the current state of every wallet whose events live in the store.

The projection is intentionally stateless — it only reads from `envelope.event_type` and `envelope.payload`. No aggregate is loaded; no repository is called. The read model is cheap to rebuild from scratch if it becomes stale or corrupted: stop the runner, clear the store, call `start()` again. This rebuilding property is unique to event sourcing — it is impossible in state-storage models because the history no longer exists.

In a production system, `_handle_envelope` would write to a real database (PostgreSQL, Redis, Elasticsearch). The `ProjectionRunner` would use a cursor stored in a checkpointing table so that restarts continue from the last processed event rather than replaying the entire stream. The projection pattern is identical regardless of the underlying storage.

!!! note "Projections vs Chapter 8 listeners"
    Chapter 8's `BalanceProjection` (Listing 8.4) was an `@event_listener` subscriber on the `InMemoryEventBus` — it reacted to events as they were published. This chapter's `BalanceLedgerProjection` reads directly from the `EventStore` — it can replay history from the beginning, catch up to the present, and continue consuming future events. Both keep a balance read model; the event-store projection is rebuildable from history; the bus listener is not.

---

## The transactional outbox

Look back at the `repo.save(wallet)` call in Listing 9.2. Three events are appended to the event store. Now suppose those events also need to reach an external broker — Kafka, RabbitMQ, another microservice. The naive approach is to call `broker.publish(envelope)` immediately after `store.append(...)`. But what if the process crashes between the append and the publish? The events are in the store but were never sent to the broker. The downstream service never learned about the deposit.

The transactional outbox pattern solves this. Instead of publishing directly, you enqueue the event into an **outbox** — a durable intermediary. The outbox persists the event alongside the aggregate's events in the same store operation. A separate background worker (the "relay") drains the outbox and forwards each event to the broker with at-least-once delivery semantics. If the relay crashes, it restarts and retries from the last unacknowledged event.

PyFly's `TransactionalOutbox` lives in `pyfly.eventsourcing`. It accepts a `publish` coroutine and a `max_attempts` limit, and exposes two methods:

- **`enqueue(envelope)`** — adds an event envelope to the outbox for delivery.
- **`start()`** — starts the background relay loop that calls `publish(envelope)` for each queued item, retrying up to `max_attempts` times on failure.

::: listing lumen/eventsourcing/outbox_demo.py | Listing 9.6 — TransactionalOutbox: reliable at-least-once delivery to a broker
from __future__ import annotations

from pyfly.eventsourcing import (
    InMemoryEventStore,
    InMemorySnapshotStore,
    TransactionalOutbox,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.eventsourcing.wallet_es import WalletES
from lumen.domain.money import Money


# Simulated broker: collect published envelopes for inspection.
_published: list = []


async def _broker_publish(envelope: object) -> None:
    _published.append(envelope)


async def demo_outbox() -> None:
    store = InMemoryEventStore()
    repo: EventSourcedRepository[WalletES] = EventSourcedRepository(
        store,
        factory=WalletES,
    )
    outbox = TransactionalOutbox(publish=_broker_publish, max_attempts=5)
    await outbox.start()

    # Open a wallet and save — events go to the store first.
    wallet = WalletES.open("w-004", "u-11", "EUR")
    wallet.deposit(Money(5000, "EUR"))
    await repo.save(wallet)

    # Enqueue the pending envelopes into the outbox.
    # In production the repository drains these automatically.
    for envelope in store.load_sync("w-004"):
        await outbox.enqueue(envelope)

    # The relay has delivered all envelopes to the broker.
    assert len(_published) == 2   # WalletOpened + FundsDeposited
:::

**How it works.** The outbox holds envelopes in a durable queue. `_broker_publish` is the delivery function — replace it with your Kafka or RabbitMQ producer. `max_attempts=5` means the relay retries a failing delivery up to five times before marking the envelope as dead-lettered.

The critical guarantee: the outbox is drained independently of the request that created the events. If the process crashes after `repo.save(wallet)` but before the outbox finishes flushing, the next process restart picks up the outbox from where it left off and completes the delivery. The aggregate state in the event store is already correct; only the broker-side delivery was interrupted.

!!! warning "At-least-once, not exactly-once"
    The outbox guarantees that every event reaches the broker *at least once*. If the relay delivers an event and then crashes before marking it as acknowledged, the event is delivered again on restart. Your broker consumers — and downstream services — must be idempotent: use the `envelope.event_id` as a deduplication key. Chapter 10 shows how Kafka and RabbitMQ consumer adapters handle deduplication automatically.

The transactional outbox is the bridge between event sourcing (this chapter) and event-driven messaging (Chapter 10). The relay that drains the outbox is the topic of the next chapter, which introduces Kafka producers and RabbitMQ exchanges and shows how to configure the outbox to deliver to them reliably.

!!! spring "Spring parity"
    The transactional outbox pattern is well known in the Spring ecosystem under the same name. Spring Modulith's `EventPublicationRegistry` and Spring's `@TransactionalEventListener(phase = AFTER_COMMIT)` approximate the same guarantee — the event is recorded durably before being dispatched. Axon Server's event store fulfils a similar role for Axon-based applications: events are written to the store first, and projection groups / event processors consume them with at-least-once guarantees from the stored log. PyFly's `TransactionalOutbox` is the portable equivalent of that pattern, without requiring a dedicated event server.

---

## Advanced: upcasting and multi-tenancy

Two advanced concerns appear in every long-lived event-sourced system. This section introduces them briefly; full treatment is beyond the scope of this book.

### Upcasting

Events are immutable. Once a `FundsDeposited` event is written to the stream, you cannot go back and add a `reference_code` field to it. But product requirements change: three months from now the finance team will want a reference code on every deposit for reconciliation. New events include it; old events do not.

An **upcaster** is a function that transforms an old event shape to the current shape during replay. The `EventStore` calls it transparently — the aggregate never sees the old shape. You register upcasters per event type and per version:

```python
# Conceptual — upcaster API varies by adapter
def upcast_funds_deposited_v1(payload: dict) -> dict:
    payload.setdefault("reference_code", "LEGACY")
    return payload
```

The upcaster runs when the `EventStore` loads an event whose schema version is lower than the current version. Old data becomes readable without a migration, and new data is written in the current schema.

### Multi-tenancy

When multiple tenants share the same event store, stream IDs must be scoped by tenant. The canonical approach is to prefix every `aggregate_id` with the tenant identifier — `"tenant-A::w-001"` rather than `"w-001"`. The `EventSourcedRepository` accepts a `tenant_id` parameter on construction; it prepends the tenant prefix to every stream operation transparently:

```python
repo_tenant_a = EventSourcedRepository(
    store,
    factory=WalletES,
    tenant_id="tenant-A",
)
```

Projections must similarly scope their read models per tenant — typically by including `tenant_id` as a column in the read-model table and filtering on it at query time.

!!! note "Choosing event sourcing"
    Event sourcing adds operational complexity — upcasters, snapshot management, projection rebuild procedures, outbox relay monitoring. Choose it deliberately for domains where auditability and time-travel queries are first-class requirements: financial ledgers, medical records, supply-chain logs. For CRUD-heavy domains where the current state is all that matters, state storage is simpler and sufficient.

---

## What you built {.recap}

Lumen's wallet is now a fully event-sourced ledger.

You saw why event sourcing fits a financial domain: the balance is a derived fact, not a stored value, and the immutable event stream gives you the full audit trail that compliance demands. You built `WalletES`, an event-sourced `AggregateRoot` that uses `apply()` to both record and immediately handle each state-changing event, keeping in-memory state and the pending-events buffer in sync at all times. You registered handlers with `when()` — registering one handler per event class — and understood the dispatch chain: `when()` first, then `on_{event_type}`, then `EventHandlerException` if neither exists.

You wired an `EventSourcedRepository` backed by `InMemoryEventStore` and `InMemorySnapshotStore`, saving a wallet with three events and reloading it from scratch to prove that replay produces identical state. You examined the `version` counter that the optimistic-concurrency guard uses: the store rejects any `append` whose `expected_version` does not match the stream's actual version, forcing the losing writer to retry from a fresh load. You configured `snapshot_interval` and saw the interval-crossing logic that triggers a snapshot even when a batch straddles the boundary — eliminating the need to replay thousands of events on every load once a stream grows long.

You built `BalanceLedgerProjection` with `FunctionProjection` and `ProjectionRunner`, keeping a fast balance read model from the raw event stream — one that can be rebuilt from history at any time, unlike the bus-listener approach from Chapter 8. Finally, you connected the event store to the broker world via `TransactionalOutbox`, which enqueues events for at-least-once delivery and retries on failure so that no fact is silently lost between the store and downstream consumers. Chapter 10 picks up exactly there, introducing the Kafka and RabbitMQ adapters that the outbox relay sends events to.

---

## Try it yourself {.exercises}

1. **Replay to a point in time.** Extend `demo_save_and_load` (Listing 9.2) to apply ten deposits of 100 cents each to `w-001`. Then load the wallet manually by calling `store.load("w-001")` (or iterating the store's event sequence), filter events to those with `occurred_at` before a specific timestamp you record after the fifth deposit, and replay only those events through a fresh `WalletES()`. Assert that the resulting balance equals 500 cents (five deposits) rather than 1 000 cents (ten deposits). This is the "time-travel query" that state-storage models cannot provide.

2. **Implement a `WalletTransferred` event and dual-aggregate save.** Add a `WalletTransferred(DomainEvent)` with fields `source_id: str`, `target_id: str`, `amount: int`, `currency: str`. Add a `transfer_to(target: WalletES, amount: Money) -> None` method to `WalletES` that calls `self.withdraw(amount)` and `target.deposit(amount)` in sequence, then applies a `WalletTransferred` event on `self`. Wire a `demo_transfer` coroutine that opens two wallets, deposits 10 000 cents into the first, transfers 3 000 cents to the second, saves both aggregates independently, reloads both from the store, and asserts that the balances are 7 000 and 3 000 cents respectively.

3. **Add a `OwnerLedgerProjection`.** Write a second `FunctionProjection` named `owner_ledger` whose handler maintains a `dict[str, list[dict]]` mapping each `owner_id` to a chronological list of transaction records. Each record should include `event_type`, `amount` (from the payload for deposit/withdraw events), and `occurred_at` (from the envelope). Feed it the same `InMemoryEventStore` used in Listing 9.5, open three wallets for the same owner, perform a mix of deposits and withdrawals, start the projection runner, and assert that the owner's transaction list has the correct number of entries in the correct chronological order.
