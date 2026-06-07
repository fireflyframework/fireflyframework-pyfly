<span class="eyebrow">Chapter 9</span>

# Event Sourcing the Ledger {.chtitle}

::: figure art/openers/ch09.svg | &nbsp;

Chapter 8 ended with a gap. The `BalanceProjection` listener keeps a fast read model by reacting to `wallet.fundsdeposited` and `wallet.fundswithdrawn` events — but the canonical state of the wallet is still a row in the `wallets` table. That row holds a single `balance` column. Every time the balance changes, the old value is gone forever. If an auditor asks "what was the balance of wallet `w-001` at 14:32 on the 3rd of March?", the answer is: you cannot know. The database remembers only the present.

Event sourcing turns this design inside out. Instead of storing the *current* state and discarding each change, you store the *sequence of changes* and derive the current state by replaying them. The `wallets` table disappears. In its place is an **event stream**: an append-only log of every `LedgerOpened`, `Credited`, and `Debited` event the ledger has ever produced. The balance at any point in time is a pure function of the events that occurred up to that moment. You can rewind to 14:32 on any date in history because you have everything that happened between then and now.

A financial ledger is the ideal domain for event sourcing. Accountants have understood for centuries that a ledger's authority comes from its entries, not from a running total scratched at the bottom of a column. The running total is a *derived fact*; the entries are the *source of truth*. PyFly's `pyfly.eventsourcing` module brings that accounting intuition into code: aggregates emit domain events, an `EventStore` records them immutably, a repository replays the stream to reconstruct state, and a `ProjectionRunner` builds read models on top.

This chapter builds the `LedgerAccount` aggregate — a separate, purpose-built event-sourced domain object that sits alongside the Chapter 6 state-stored `Wallet`. You will see every component of the `pyfly.eventsourcing` module and how the event store, snapshotting, projections, and the transactional outbox work together to give Lumen a ledger that is both auditable and performant.

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
| led-001 | 1 | LedgerOpened | `{"currency":"EUR","owner_id":"u-42"}` | 2026-03-01 09:00 |
| led-001 | 2 | Credited | `{"amount":10000,"balance":10000}` | 2026-03-01 09:01 |
| led-001 | 3 | Debited | `{"amount":1500,"balance":8500}` | 2026-03-03 17:11 |

The current balance is still 85.00 EUR — but now you can read every decision that led to it. An auditor, a regulator, a fraud investigator can replay the stream from any offset and see exactly what happened and when.

::: figure art/figures/09-eventsourcing.svg | Figure 9.1 — State storage vs event storage: one model keeps a snapshot of the present; the other keeps every fact that led to it.

The trade-off is real. Event storage makes reads more expensive by default — you must replay the stream to learn the current balance — and it requires discipline around schema evolution (events are immutable; you cannot change a field name after the fact). Both concerns have solutions in PyFly: **snapshots** accelerate replay of long streams, and **upcasters** translate old event shapes to new ones during load. You will see both before the end of this chapter.

!!! note "Events as the system of record"
    Event sourcing is not the same as event-driven architecture. Chapter 8 used EDA: the aggregate stored its state normally and published domain events as a side effect. Event sourcing goes further: the events *are* the state. There is no separate `balance` column to keep in sync — the balance is computed by the repository every time it loads the aggregate.

---

## A separate base for event-sourced aggregates

Before writing a line of ledger code, a naming distinction matters. Chapter 6 built `Wallet` on top of `pyfly.domain.AggregateRoot` — the state-stored base class. Chapter 9's `LedgerAccount` uses a **different** base class: `pyfly.eventsourcing.AggregateRoot`. The two live in separate packages and are deliberately unrelated.

| Concern | Chapter 6 `Wallet` | Chapter 9 `LedgerAccount` |
|---|---|---|
| Base class | `pyfly.domain.AggregateRoot` | `pyfly.eventsourcing.AggregateRoot` |
| Domain event | `pyfly.domain.DomainEvent` | `pyfly.eventsourcing.DomainEvent` |
| State lives in | a database row | the event stream |
| Repository | `WalletRepository` (R2DBC) | `LedgerAccountRepository` (`EventSourcedRepository`) |

The `pyfly.eventsourcing.AggregateRoot` gives the aggregate the event-sourcing machinery:

- **`when(EventType, handler)`** — registers a handler for a given event class. The handler receives `(aggregate, event)` as two arguments and performs the mutation. It is a plain callable — a lambda, a free function, or a one-liner that delegates to a private method.
- **`apply(event)`** — routes a brand-new event through its registered handler and queues it for the event store. Both happen atomically: the in-memory state updates immediately without a round-trip through the store.
- **`replay(event_type, event)`** — re-runs a persisted event through the same handler *without* re-queuing it. The repository calls this on load to rebuild state from the stored stream.
- **`version`** — an integer counter incremented after each dispatched event. The store uses this as the optimistic-concurrency token.

The dispatch order is: registered `when()` handler first; then a method named `on_{event_type}` if one exists on the aggregate; if neither exists, `EventHandlerException` is raised — a missing handler would silently corrupt reconstructed state, so the aggregate fails loudly.

!!! warning "Two-arg handler — the most common trap"
    Every handler registered with `when()` is called as `handler(aggregate, event)` — two arguments. A bound method like `self._on_opened` has signature `(self, event)`, which makes it a one-arg callable from the outside. Passing a bound method directly causes a `TypeError` at runtime. The pattern used throughout this chapter is a one-liner lambda — `lambda agg, evt: agg._on_opened(evt)` — which is correctly two-arg and keeps the real logic in a private method where it can be unit-tested and type-checked independently.

---

## The event-sourced aggregate

In Chapter 6 the `Wallet` aggregate held `_balance: Money` as direct Python state. A `deposit` call added to it; a `withdraw` call subtracted from it. In the event-sourced version, the aggregate never mutates its own fields directly. Instead, every state change is mediated by a domain event: the behaviour method *applies* the event, the event handler *updates the fields*, and the `EventStore` persists the event. When the aggregate is loaded, the repository replays all historical events through the same handlers, rebuilding the in-memory state event by event.

This two-step indirection — apply then handle — is the core mechanic of event sourcing. It creates a strict discipline: every state transition is recorded exactly once, as an event, and the aggregate's current state is always provable from its history.

**Zero-arg constructor.** One important difference from Chapter 6 is that `LedgerAccount.__init__` takes no arguments. This is required because `EventSourcedRepository` calls the factory as `LedgerAccount()` and then assigns `.id` before replaying the stream. You never construct a new ledger by passing arguments to `__init__` — you call the `open` classmethod instead.

Here are the events and the aggregate:

::: listing lumen/models/entities/v1/ledger_account.py | Listing 9.1 — LedgerAccount: an event-sourced aggregate that derives its balance from replay
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import BusinessRuleViolation
from pyfly.eventsourcing import AggregateRoot, DomainEvent


# --- Domain events — the durable facts of the ledger -----------------

@dataclass
class LedgerOpened(DomainEvent):
    """The ledger was opened for an owner in a single currency."""
    account_id: str = ""
    owner_id: str = ""
    currency: str = ""


@dataclass
class Credited(DomainEvent):
    """Money moved *into* the ledger (a deposit / inbound transfer leg)."""
    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


@dataclass
class Debited(DomainEvent):
    """Money moved *out of* the ledger (withdrawal / outbound transfer leg)."""
    account_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


# --- Event-sourced aggregate root ------------------------------------

class LedgerAccount(AggregateRoot):
    """An event-sourced money-movement ledger.

    Zero-arg constructible so it can serve as the repository's factory —
    EventSourcedRepository calls LedgerAccount() then assigns .id before
    replaying the stream. Use the open() classmethod to create new ledgers.
    """

    def __init__(self) -> None:
        super().__init__()
        self.owner_id: str = ""
        self.currency: Currency = Currency.EUR
        self.balance: Money = Money.zero(Currency.EUR)
        # Register apply-handlers. _dispatch calls handler(aggregate, event).
        # Use a lambda so the callable is two-arg; delegate to a private
        # method to keep the real logic type-checked and unit-testable.
        self.when(LedgerOpened, lambda agg, evt: agg._on_opened(evt))
        self.when(Credited,     lambda agg, evt: agg._on_credited(evt))
        self.when(Debited,      lambda agg, evt: agg._on_debited(evt))

    # --- factory ---------------------------------------------------------

    @classmethod
    def open(
        cls, account_id: str, owner_id: str, currency: Currency
    ) -> "LedgerAccount":
        """Open a new empty ledger; appends LedgerOpened."""
        if not owner_id.strip():
            raise BusinessRuleViolation(
                "ledger-owner-required", "owner_id is required"
            )
        account = cls()
        account.id = account_id
        account.apply(
            LedgerOpened(
                account_id=account_id,
                owner_id=owner_id,
                currency=currency.value,
            )
        )
        return account

    # --- commands (validate invariants, then apply) -----------------------

    def credit(self, amount: Money) -> None:
        """Record money entering the ledger; appends Credited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "ledger-credit-positive", "credit amount must be > 0"
            )
        new_balance = self.balance.add(amount)
        self.apply(
            Credited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=new_balance.amount,
            )
        )

    def debit(self, amount: Money) -> None:
        """Record money leaving; refuses to overdraw. Appends Debited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "ledger-debit-positive", "debit amount must be > 0"
            )
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "ledger-insufficient-funds",
                f"cannot debit {amount}; balance is {self.balance}",
            )
        self.apply(
            Debited(
                account_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=remaining.amount,
            )
        )

    # --- apply-handlers (pure folds, shared by apply + replay) -----------

    def _on_opened(self, event: object) -> None:
        self.owner_id = event.owner_id          # type: ignore[attr-defined]
        self.currency = Currency(event.currency) # type: ignore[attr-defined]
        self.balance = Money.zero(self.currency)

    def _on_credited(self, event: object) -> None:
        self.balance = Money(
            event.balance, Currency(event.currency)  # type: ignore[attr-defined]
        )

    def _on_debited(self, event: object) -> None:
        self.balance = Money(
            event.balance, Currency(event.currency)  # type: ignore[attr-defined]
        )

    # --- helpers ---------------------------------------------------------

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.currency:
            raise BusinessRuleViolation(
                "ledger-currency-mismatch",
                f"ledger holds {self.currency.value}, "
                f"got {amount.currency.value}",
            )
:::

**How it works.** The `__init__` registers three `when()` handlers — one per event class — each via a two-arg lambda that delegates to a private method. No arithmetic happens inside the handlers; the behaviour methods (`credit`, `debit`) are responsible for all validation and for computing the new state before constructing the event. The handler simply *applies* the already-computed result.

`account.apply(Credited(...))` does two things atomically: it appends the event to the pending-events buffer (so the repository can persist it) and immediately dispatches it to the `Credited` handler (so `balance` updates in memory). This means the aggregate's in-memory state is always consistent with its pending events, even before a save.

The factory method `open` calls `apply(LedgerOpened(...))` rather than setting fields directly. This matters: if you ever loaded this aggregate from its event stream, the `LedgerOpened` event would pass through exactly the same `_on_opened` handler, and the result would be identical. State and replay are identical code paths — that symmetry is the correctness guarantee of event sourcing.

The `version` counter on `AggregateRoot` starts at zero and increments after each dispatched event. After `open`, `account.version == 1`. After one credit, `account.version == 2`. You will see this number again when the `EventStore` enforces optimistic concurrency.

The `pending_events()` method returns the list of events queued since the last save. The tests drive the aggregate in isolation before wiring the repository, which makes the invariant easy to verify:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.2 — Unit tests: aggregate in isolation, commands and invariants
def test_open_emits_ledger_opened_and_starts_empty() -> None:
    account = LedgerAccount.open(
        "led-1", owner_id="owner-1", currency=Currency.EUR
    )
    assert account.id == "led-1"
    assert account.owner_id == "owner-1"
    assert account.currency is Currency.EUR
    assert account.balance == Money.zero(Currency.EUR)
    # apply() queued the event for the store and bumped the version.
    [event] = account.pending_events()
    assert isinstance(event, LedgerOpened)
    assert event.account_id == "led-1"
    assert event.currency == "EUR"
    assert account.version == 1


def test_credit_then_debit_track_the_balance() -> None:
    account = LedgerAccount.open("led-2", owner_id="o", currency=Currency.EUR)
    account.credit(Money(1000, Currency.EUR))
    account.debit(Money(400, Currency.EUR))
    assert account.balance == Money(600, Currency.EUR)
    kinds = [type(e).__name__ for e in account.pending_events()]
    assert kinds == ["LedgerOpened", "Credited", "Debited"]
    assert account.version == 3


def test_debit_cannot_overdraw() -> None:
    account = LedgerAccount.open("led-3", owner_id="o", currency=Currency.EUR)
    account.credit(Money(500, Currency.EUR))
    with pytest.raises(BusinessRuleViolation) as exc:
        account.debit(Money(501, Currency.EUR))
    assert exc.value.rule == "ledger-insufficient-funds"
    # Invariant held: balance unchanged, no Debited event queued.
    assert account.balance == Money(500, Currency.EUR)
    assert [type(e).__name__ for e in account.pending_events()] == [
        "LedgerOpened",
        "Credited",
    ]
:::

!!! tip "on_{event_type} as an alternative"
    Instead of `when()` lambdas, you can define a method on the aggregate named after the event in snake_case — `on_ledgeropened(self, evt)` is discovered automatically. Use `when()` for concise one-liners and named methods for handlers that need multiple statements or local variables. The dispatch order is: `when()` handler first; then `on_{event_type}` method; then `EventHandlerException` if neither exists.

!!! spring "Spring parity"
    `AggregateRoot` + `apply()` + `when()` is PyFly's equivalent of Axon Framework's `@Aggregate` + `AggregateLifecycle.apply(event)` + `@EventSourcingHandler`. Axon uses annotation-driven handler discovery (`@EventSourcingHandler`); PyFly uses `when()` registration or `on_*` method convention. The replaying mechanic — load events from the store, call the same handlers, rebuild state — is identical in both frameworks.

---

## The EventStore

The aggregate knows how to produce and replay events. The `EventStore` knows how to persist and retrieve them. These are deliberately separate concerns: the aggregate is pure business logic with no I/O; the event store is pure I/O with no business logic.

The `EventStore` protocol exposes two core operations:

- **`append(aggregate_id, aggregate_type, events, *, expected_version)`** — persists a batch of events for an aggregate stream. Raises `ConcurrencyError` if the stream's actual version does not match `expected_version`.
- **`load(aggregate_id, *, after_sequence=0)`** — returns the ordered sequence of `StoredEventEnvelope` objects for an aggregate, from the first (or from `after_sequence`) to the most recent.

`InMemoryEventStore` is the out-of-the-box implementation. Like `InMemoryEventBus` in Chapter 8, it runs entirely in-process with no I/O — perfect for development and tests. A production deployment would swap in a PostgreSQL- or EventStoreDB-backed adapter.

The `EventSourcedRepository` wraps the `EventStore` and handles the full save/load cycle. You never interact with the `EventStore` directly from application code; you call `repo.save(aggregate)` and `repo.load(aggregate_id)`, and the repository handles the rest.

All imports come from two locations:

```python
# Core event-sourcing types — all in the base package
from pyfly.eventsourcing import (
    AggregateRoot,
    DomainEvent,
    EventStore,
    InMemoryEventStore,
    SnapshotStore,
    InMemorySnapshotStore,
    StoredEventEnvelope,
)
# The generic repository lives in the .repository submodule
from pyfly.eventsourcing.repository import EventSourcedRepository
```

---

## The LedgerAccountRepository

The `EventSourcedRepository` generic class handles the full save/load cycle. You subclass it for two reasons: to pass the concrete factory and snapshot store in a single well-named constructor, and — optionally — to override `_envelope_to_event` so that replayed events are real typed dataclasses rather than the generic attribute-bag the base class produces.

::: listing lumen/models/repositories/ledger_repository.py | Listing 9.3 — LedgerAccountRepository: typed replay via _envelope_to_event
from __future__ import annotations

from typing import ClassVar

from lumen.models.entities.v1.ledger_account import (
    Credited,
    Debited,
    LedgerAccount,
    LedgerOpened,
)
from pyfly.eventsourcing import (
    DomainEvent,
    EventStore,
    SnapshotStore,
    StoredEventEnvelope,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

# Map a stored event_type (the event class name) back to its dataclass.
_EVENT_TYPES: dict[str, type[DomainEvent]] = {
    LedgerOpened.__name__: LedgerOpened,
    Credited.__name__:     Credited,
    Debited.__name__:      Debited,
}


class LedgerAccountRepository(EventSourcedRepository[LedgerAccount]):
    """Loads/saves LedgerAccount aggregates via the event store."""

    SNAPSHOT_INTERVAL: ClassVar[int] = 100

    def __init__(
        self,
        store: EventStore,
        *,
        snapshots: SnapshotStore | None = None,
    ) -> None:
        super().__init__(
            store,
            factory=LedgerAccount,
            snapshots=snapshots,
            snapshot_interval=self.SNAPSHOT_INTERVAL,
        )

    @staticmethod
    def _envelope_to_event(envelope: StoredEventEnvelope) -> object:
        """Rebuild the concrete event dataclass from a stored payload.

        Overrides the base-class generic hydration so that replayed events
        are the same dataclasses the aggregate applied on the write side.
        Unknown fields are ignored for forward-compatibility.
        """
        event_cls = _EVENT_TYPES.get(envelope.event_type)
        if event_cls is None:
            # Fall back to generic hydration for unrecognised event types.
            return EventSourcedRepository._envelope_to_event(envelope)
        field_names = {
            f.name for f in event_cls.__dataclass_fields__.values()
        }
        kwargs = {
            k: v for k, v in envelope.payload.items() if k in field_names
        }
        return event_cls(**kwargs)
:::

**How it works.** `super().__init__(store, factory=LedgerAccount, ...)` tells the base repository to call `LedgerAccount()` — no arguments — to create a blank aggregate, then assign `.id`, then replay the stream. The `factory` parameter accepts any zero-arg callable; passing the class itself (`factory=LedgerAccount`) is equivalent to `factory=lambda: LedgerAccount()`.

The `_envelope_to_event` override looks up the stored `event_type` string in the `_EVENT_TYPES` map, uses `__dataclass_fields__` to filter the payload to known fields, and reconstructs the real dataclass. Unknown fields are silently dropped — this is forward-compatibility: if a future event version adds a field the old handler does not know about, the ledger keeps replaying instead of crashing. The base-class fallback at the bottom handles any event type the ledger does not recognise.

---

## Save, load, and the replay proof

Here is the complete save-and-load cycle from the test suite:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.4 — Headline test: balance survives a reload by replay
@pytest.mark.asyncio
async def test_balance_survives_reload_by_replay(
    event_store: InMemoryEventStore,
) -> None:
    # 1. Open + credit + debit, then persist the pending events.
    account = LedgerAccount.open(
        "acct-1", owner_id="owner-7", currency=Currency.EUR
    )
    account.credit(Money(2500, Currency.EUR))  # +25.00
    account.debit(Money(1000, Currency.EUR))   # -10.00 -> 15.00
    assert account.balance == Money(1500, Currency.EUR)

    repo = LedgerAccountRepository(event_store)
    await repo.save(account)
    # After committing, the aggregate has no pending events left.
    assert account.pending_events() == []

    # 2. Reconstruct from a *fresh* repository + aggregate. Nothing here
    #    carries the in-memory object's state — load() rebuilds the
    #    LedgerAccount purely by replaying the stored event stream.
    fresh_repo = LedgerAccountRepository(event_store)
    recovered = await fresh_repo.load("acct-1")

    assert recovered is not None
    assert recovered is not account
    assert recovered.owner_id == "owner-7"
    assert recovered.currency is Currency.EUR
    # The load-by-replay proof: balance recomputed from events, not stored.
    assert recovered.balance == Money(1500, Currency.EUR)
    # Three events were folded in: LedgerOpened, Credited, Debited.
    assert recovered.version == 3
    # A reconstructed aggregate has nothing pending — it was not "changed".
    assert recovered.pending_events() == []
:::

**How it works.** `repo.save(account)` calls `store.append("acct-1", "LedgerAccount", pending_events, expected_version=0)`. The three events — `LedgerOpened`, `Credited`, `Debited` — are serialized into `StoredEventEnvelope` objects and written to the stream in order. The pending-events buffer on the aggregate is cleared.

`fresh_repo.load("acct-1")` calls `store.load("acct-1")`, receives the three envelopes, constructs a blank `LedgerAccount()` via the factory, assigns `.id = "acct-1"`, and calls `_envelope_to_event` on each envelope before replaying it. The `_on_opened` / `_on_credited` / `_on_debited` handlers run in sequence, and after all three `recovered.balance.amount` is `1500` — the same value the live aggregate held after the three operations, computed without any shared state between the two objects.

The test uses *two independent repository instances* sharing the same in-memory store — `repo` for the write and `fresh_repo` for the read. This proves that replay, not in-process object identity, is the source of truth.

The event store also keeps the raw envelopes accessible for inspection — useful for audits and tests:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.5 — The store holds the immutable event stream
@pytest.mark.asyncio
async def test_store_holds_the_immutable_event_stream(
    event_store: InMemoryEventStore,
) -> None:
    account = LedgerAccount.open(
        "acct-2", owner_id="o", currency=Currency.GBP
    )
    account.credit(Money(800, Currency.GBP))
    account.debit(Money(300, Currency.GBP))
    await LedgerAccountRepository(event_store).save(account)

    envelopes = await event_store.load("acct-2")
    assert [e.event_type for e in envelopes] == [
        "LedgerOpened", "Credited", "Debited"
    ]
    assert [e.sequence for e in envelopes] == [1, 2, 3]
    assert all(e.aggregate_type == "LedgerAccount" for e in envelopes)
    # The Debited payload carries the post-debit running balance.
    assert envelopes[2].payload["balance"] == 500
    assert await event_store.latest_version("acct-2") == 3
:::

!!! note "Factory argument"
    `EventSourcedRepository` accepts a `factory` callable that produces a blank aggregate instance. The factory must return an aggregate in its initial `__init__` state — no events applied — because the repository will apply the full history itself. Passing a factory that constructs an already-mutated aggregate (for example, one that calls `open()` internally) will corrupt the replay: `_on_opened` would run twice, the second time overwriting the fields that the real events already set.

---

## Optimistic concurrency

Two concurrent requests — a credit from the mobile app and an automated fee debit from a background job — can load the same ledger at the same version, each apply their own change, and attempt to save. Without a concurrency guard, one of the saves silently wins and the other's events are lost. The resulting stream is internally inconsistent: the sequence numbers collide, the balance is wrong, and neither request received an error.

Optimistic concurrency prevents this. Before appending new events, the `EventStore` compares the stream's *current* version against the *expected* version the repository read at load time. If they match, the append proceeds and the stream version advances. If they do not match — because another writer already appended events — `ConcurrencyError` is raised and the losing request must retry from a fresh load.

The `expected_version` is passed implicitly by the repository: it remembers the version at which it loaded the aggregate and passes it to the store on save. You do not need to manage version numbers manually in application code.

The version progression is straightforward: after a save-and-reload the version is preserved, and further writes advance it without conflict:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.6 — Continuing to append after a reload: optimistic lock advances correctly
@pytest.mark.asyncio
async def test_continues_appending_after_a_reload(
    event_store: InMemoryEventStore,
) -> None:
    repo = LedgerAccountRepository(event_store)
    account = LedgerAccount.open(
        "acct-3", owner_id="o", currency=Currency.EUR
    )
    account.credit(Money(1000, Currency.EUR))
    await repo.save(account)

    # Reload, mutate again, save again — version must advance, no conflict.
    reloaded = await repo.load("acct-3")
    assert reloaded is not None
    assert reloaded.version == 2
    reloaded.debit(Money(250, Currency.EUR))
    await repo.save(reloaded)

    final = await repo.load("acct-3")
    assert final is not None
    assert final.balance == Money(750, Currency.EUR)
    assert final.version == 3
    assert await event_store.latest_version("acct-3") == 3
:::

**How it works.** After the first save the stream is at version 2. `reloaded.version == 2` when loaded. `repo.save(reloaded)` appends with `expected_version=2`; the store advances to 3 and succeeds. The `final` load replays all three events and confirms the balance is correct.

!!! warning "Always handle ConcurrencyError"
    When two writers race, the losing save raises `ConcurrencyError`. Your application service must catch it and decide what to do: retry the full load-mutate-save cycle (appropriate for low-contention writes), or surface a 409 Conflict to the caller (appropriate when the caller should re-submit with fresh data). Never silently swallow the error — a swallowed concurrency error leaves the stream in an inconsistent state.

---

## Snapshots

Event sourcing trades write simplicity for read cost. Loading a ledger that has recorded 10 000 money movements means replaying 10 000 events through the apply chain every time the aggregate is needed. For most ledgers the stream is short; for high-frequency accounts it can grow prohibitively long.

Snapshots address this. A snapshot is a serialized checkpoint of the aggregate's state captured at a specific version. When the repository loads an aggregate, it first looks for the most recent snapshot and, if found, deserializes the state directly to that version — then replays only the events that arrived after the snapshot. A snapshot at version 9 000 reduces a 10 000-event replay to 1 000 events.

PyFly's `InMemorySnapshotStore` stores snapshots in memory. You pass it to `EventSourcedRepository` alongside the event store via the `snapshots` keyword argument — exactly as `LedgerAccountRepository.__init__` accepts it:

```python
store = InMemoryEventStore()
snapshots = InMemorySnapshotStore()
repo = LedgerAccountRepository(store, snapshots=snapshots)
```

The repository decides *when* to snapshot automatically using `snapshot_interval` (default `100`, the value of `LedgerAccountRepository.SNAPSHOT_INTERVAL`). After every `save`, it checks whether the aggregate's new version **crosses** a multiple of `snapshot_interval`:

```python
# crosses_interval is True when this batch pushes the stream past a
# 100-event boundary — e.g., version 95 → 105 crosses the 100 mark.
crossed = (
    (aggregate.version // snapshot_interval)
    > (previous_version // snapshot_interval)
)
```

This interval-crossing logic (rather than exact divisibility) handles the common case where a single save batch straddles the threshold — for example, a bulk import that adds 10 events takes the version from 95 to 105 and correctly triggers a snapshot even though neither 95 nor 105 is exactly divisible by 100.

The snapshot seam is harmless when the stream is shorter than the interval — the test below proves it:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.7 — Snapshot store wired: reload still yields correct state
@pytest.mark.asyncio
async def test_snapshot_store_round_trips_the_ledger() -> None:
    """With a snapshot store wired, reload still yields the right state.

    The ledger's stream is far shorter than the snapshot interval, so this
    proves the snapshot seam is harmless and the repository falls back
    to a full replay.
    """
    store = InMemoryEventStore()
    snapshots = InMemorySnapshotStore()
    repo = LedgerAccountRepository(store, snapshots=snapshots)

    account = LedgerAccount.open(
        "acct-4", owner_id="o", currency=Currency.EUR
    )
    account.credit(Money(5000, Currency.EUR))
    await repo.save(account)

    recovered = await repo.load("acct-4")
    assert recovered is not None
    assert recovered.balance == Money(5000, Currency.EUR)
:::

**How it works.** After the save, the repository checks: does `version 2 // 100 > 0 // 100`? No — the snapshot threshold has not been crossed yet, so no snapshot is taken. The next `load` performs a full replay of the two events and returns the correct balance. Once a ledger does cross a 100-event boundary, the repository serializes the aggregate's state into a snapshot envelope. The next load finds the snapshot, deserializes directly to that version, and then asks the event store for events with a sequence number greater than the snapshot version — reducing replay cost to only the delta.

!!! tip "Snapshot interval in production"
    A `snapshot_interval` of 100 is the default and a sensible starting point. For high-frequency ledgers you might lower it; for accounts that only change a few times a day, a higher interval reduces snapshot-storage cost. Snapshots are an optimization, not a correctness requirement — removing them leaves the system correct but slower.

---

## Boot wiring and auto-configuration

The `pyfly.eventsourcing` module is in the **base** `pyfly` package — no extra dependency. Enabling it requires two steps: set `pyfly.eventsourcing.enabled: true` in `pyfly.yaml` and annotate the application with `@enable_domain_stack`. PyFly's auto-configuration then registers `event_store` and `snapshot_store` beans automatically.

The test suite verifies this directly:

::: listing tests/test_ledger_event_sourcing.py | Listing 9.8 — Auto-configuration registers the event store beans
def test_auto_configuration_registers_event_store_beans() -> None:
    """enable_domain_stack activates this auto-config when
    pyfly.eventsourcing.enabled=true, registering the in-memory
    event/snapshot stores the ledger repository depends on."""
    from pyfly.eventsourcing.auto_configuration import (
        EventSourcingAutoConfiguration,
    )

    config = EventSourcingAutoConfiguration()
    assert isinstance(config.event_store(), InMemoryEventStore)
    assert isinstance(config.snapshot_store(), InMemorySnapshotStore)
:::

In application code the repository is wired via dependency injection:

```python
# pyfly.yaml
pyfly:
  eventsourcing:
    enabled: true
```

```python
# In a service or handler — the beans are injected automatically
@component
class LedgerService:
    def __init__(
        self,
        event_store: EventStore,
        snapshot_store: SnapshotStore,
    ) -> None:
        self._repo = LedgerAccountRepository(
            event_store, snapshots=snapshot_store
        )
```

---

## Projections

The event store is the system of record. But most application queries — "what is the current balance?", "show all ledgers for owner u-42", "which accounts are above 1 000 EUR?" — should not replay event streams on every read. They should hit a pre-computed read model: a table optimized for queries, kept in sync by a background process that consumes the event stream.

That background process is a **projection**. A projection subscribes to the event stream and updates a read model every time a relevant event arrives. PyFly provides `FunctionProjection` and `ProjectionRunner` in `pyfly.eventsourcing.projection`.

- **`FunctionProjection(name, handler_fn)`** wraps an async function that receives one `StoredEventEnvelope` and updates the read model.
- **`ProjectionRunner(projection, store)`** drives the projection: it polls the `EventStore` for new events and calls the projection handler for each one.

Here is a `BalanceLedgerProjection` that builds a balance read model from the ledger's event stream:

::: listing lumen/eventsourcing/balance_projection.py | Listing 9.9 — BalanceLedgerProjection: a read model built from the event stream
from __future__ import annotations

from pyfly.eventsourcing import InMemoryEventStore
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner


# The in-process read model — in production, replace with a DB table.
_balance_store: dict[str, dict] = {}


async def _handle_envelope(envelope: object) -> None:
    """Update the balance read model for each ledger event."""
    event_type: str = getattr(envelope, "event_type", "")
    payload: dict = getattr(envelope, "payload", {})

    if event_type == "LedgerOpened":
        _balance_store[payload["account_id"]] = {
            "account_id": payload["account_id"],
            "owner_id": payload.get("owner_id", ""),
            "balance_cents": 0,
            "currency": payload.get("currency", ""),
        }
    elif event_type in ("Credited", "Debited"):
        account_id = payload["account_id"]
        if account_id in _balance_store:
            _balance_store[account_id]["balance_cents"] = (
                payload["balance"]
            )


def build_projection(store: InMemoryEventStore) -> ProjectionRunner:
    projection = FunctionProjection("balance_ledger", _handle_envelope)
    return ProjectionRunner(projection, store)


async def demo_projection(store: InMemoryEventStore) -> None:
    runner = build_projection(store)
    await runner.start()

    balance = _balance_store.get("led-001", {})
    print(f"Balance read model: {balance}")
:::

**How it works.** `FunctionProjection("balance_ledger", _handle_envelope)` wraps the async handler. `ProjectionRunner(projection, store)` links it to the `InMemoryEventStore`. Calling `await runner.start()` causes the runner to iterate every envelope in the store — in sequence-number order — and call `_handle_envelope` for each. After `start()` returns, `_balance_store` reflects the current state of every ledger whose events live in the store.

The projection is intentionally stateless — it only reads from `envelope.event_type` and `envelope.payload`. No aggregate is loaded; no repository is called. The read model is cheap to rebuild from scratch if it becomes stale or corrupted: stop the runner, clear the store, call `start()` again. This rebuilding property is unique to event sourcing — it is impossible in state-storage models because the history no longer exists.

In a production system, `_handle_envelope` would write to a real database (PostgreSQL, Redis, Elasticsearch). The `ProjectionRunner` would use a cursor stored in a checkpointing table so that restarts continue from the last processed event rather than replaying the entire stream. The projection pattern is identical regardless of the underlying storage.

!!! note "Projections vs Chapter 8 listeners"
    Chapter 8's `BalanceProjection` (Listing 8.4) was an `@event_listener` subscriber on the `InMemoryEventBus` — it reacted to events as they were published. This chapter's `BalanceLedgerProjection` reads directly from the `EventStore` — it can replay history from the beginning, catch up to the present, and continue consuming future events. Both keep a balance read model; the event-store projection is rebuildable from history; the bus listener is not.

---

## The transactional outbox

Look back at the `repo.save(account)` call in Listing 9.4. Three events are appended to the event store. Now suppose those events also need to reach an external broker — Kafka, RabbitMQ, another microservice. The naive approach is to call `broker.publish(envelope)` immediately after `store.append(...)`. But what if the process crashes between the append and the publish? The events are in the store but were never sent to the broker. The downstream service never learned about the credit.

The transactional outbox pattern solves this. Instead of publishing directly, you enqueue the event into an **outbox** — a durable intermediary. The outbox persists the event alongside the aggregate's events in the same store operation. A separate background worker (the "relay") drains the outbox and forwards each event to the broker with at-least-once delivery semantics. If the relay crashes, it restarts and retries from the last unacknowledged event.

PyFly's `TransactionalOutbox` lives in `pyfly.eventsourcing`. It accepts a `publish` coroutine and a `max_attempts` limit, and exposes two methods:

- **`enqueue(envelope)`** — adds an event envelope to the outbox for delivery.
- **`start()`** — starts the background relay loop that calls `publish(envelope)` for each queued item, retrying up to `max_attempts` times on failure.

::: listing lumen/eventsourcing/outbox_demo.py | Listing 9.10 — TransactionalOutbox: reliable at-least-once delivery to a broker
from __future__ import annotations

from pyfly.eventsourcing import (
    InMemoryEventStore,
    InMemorySnapshotStore,
    TransactionalOutbox,
)
from pyfly.eventsourcing.repository import EventSourcedRepository

from lumen.models.entities.v1.ledger_account import LedgerAccount
from lumen.models.entities.v1.money import Money
from lumen.interfaces.enums.v1.currency import Currency


# Simulated broker: collect published envelopes for inspection.
_published: list = []


async def _broker_publish(envelope: object) -> None:
    _published.append(envelope)


async def demo_outbox() -> None:
    store = InMemoryEventStore()
    repo = LedgerAccountRepository(store)
    outbox = TransactionalOutbox(publish=_broker_publish, max_attempts=5)
    await outbox.start()

    account = LedgerAccount.open("led-004", "u-11", Currency.EUR)
    account.credit(Money(5000, Currency.EUR))
    await repo.save(account)

    # Enqueue the stored envelopes into the outbox.
    for envelope in await store.load("led-004"):
        await outbox.enqueue(envelope)

    # The relay has delivered all envelopes to the broker.
    assert len(_published) == 2   # LedgerOpened + Credited
:::

**How it works.** The outbox holds envelopes in a durable queue. `_broker_publish` is the delivery function — replace it with your Kafka or RabbitMQ producer. `max_attempts=5` means the relay retries a failing delivery up to five times before marking the envelope as dead-lettered.

The critical guarantee: the outbox is drained independently of the request that created the events. If the process crashes after `repo.save(account)` but before the outbox finishes flushing, the next process restart picks up the outbox from where it left off and completes the delivery. The aggregate state in the event store is already correct; only the broker-side delivery was interrupted.

!!! warning "At-least-once, not exactly-once"
    The outbox guarantees that every event reaches the broker *at least once*. If the relay delivers an event and then crashes before marking it as acknowledged, the event is delivered again on restart. Your broker consumers — and downstream services — must be idempotent: use the `envelope.event_id` as a deduplication key. Chapter 10 shows how Kafka and RabbitMQ consumer adapters handle deduplication automatically.

The transactional outbox is the bridge between event sourcing (this chapter) and event-driven messaging (Chapter 10). The relay that drains the outbox is the topic of the next chapter, which introduces Kafka producers and RabbitMQ exchanges and shows how to configure the outbox to deliver to them reliably.

!!! spring "Spring parity"
    The transactional outbox pattern is well known in the Spring ecosystem under the same name. Spring Modulith's `EventPublicationRegistry` and Spring's `@TransactionalEventListener(phase = AFTER_COMMIT)` approximate the same guarantee — the event is recorded durably before being dispatched. Axon Server's event store fulfils a similar role for Axon-based applications: events are written to the store first, and projection groups / event processors consume them with at-least-once guarantees from the stored log. PyFly's `TransactionalOutbox` is the portable equivalent of that pattern, without requiring a dedicated event server.

---

## Advanced: upcasting and multi-tenancy

Two advanced concerns appear in every long-lived event-sourced system. This section introduces them briefly; full treatment is beyond the scope of this book.

### Upcasting

Events are immutable. Once a `Credited` event is written to the stream, you cannot go back and add a `reference_code` field to it. But product requirements change: three months from now the finance team will want a reference code on every credit for reconciliation. New events include it; old events do not.

An **upcaster** is a function that transforms an old event shape to the current shape during replay. The `EventStore` calls it transparently — the aggregate never sees the old shape. You register upcasters per event type and per version:

```python
# Conceptual — upcaster API varies by adapter
def upcast_credited_v1(payload: dict) -> dict:
    payload.setdefault("reference_code", "LEGACY")
    return payload
```

The upcaster runs when the `EventStore` loads an event whose schema version is lower than the current version. Old data becomes readable without a migration, and new data is written in the current schema.

### Multi-tenancy

When multiple tenants share the same event store, stream IDs must be scoped by tenant. The canonical approach is to prefix every `aggregate_id` with the tenant identifier — `"tenant-A::led-001"` rather than `"led-001"`. The `EventSourcedRepository` accepts a `tenant_id` parameter on construction; it prepends the tenant prefix to every stream operation transparently:

```python
repo_tenant_a = EventSourcedRepository(
    store,
    factory=LedgerAccount,
    tenant_id="tenant-A",
)
```

Projections must similarly scope their read models per tenant — typically by including `tenant_id` as a column in the read-model table and filtering on it at query time.

!!! note "Choosing event sourcing"
    Event sourcing adds operational complexity — upcasters, snapshot management, projection rebuild procedures, outbox relay monitoring. Choose it deliberately for domains where auditability and time-travel queries are first-class requirements: financial ledgers, medical records, supply-chain logs. For CRUD-heavy domains where the current state is all that matters, state storage is simpler and sufficient.

---

## What you built {.recap}

Lumen's `LedgerAccount` is now a fully event-sourced ledger that coexists with the Chapter 6 state-stored `Wallet`.

You saw why the two use separate base classes: `Wallet` extends `pyfly.domain.AggregateRoot` and stores its balance in a database row; `LedgerAccount` extends `pyfly.eventsourcing.AggregateRoot` and derives its balance from an immutable event stream. The distinction is not just cosmetic — the event-sourcing machinery (`apply`, `replay`, `when`) lives on the eventsourcing base class only.

You built the three domain events — `LedgerOpened`, `Credited`, `Debited` — as `dataclass`es that extend `pyfly.eventsourcing.DomainEvent`, each with default field values so the repository can reconstruct them from a stored payload. You registered handlers with `when()` — each a two-arg lambda delegating to a private method — and understood the bound-method trap: a bound method is only one-arg from the outside, which causes a `TypeError` at dispatch time.

You wired `LedgerAccountRepository`, a thin subclass of `EventSourcedRepository[LedgerAccount]`, that passes the zero-arg `LedgerAccount` factory and overrides `_envelope_to_event` to hydrate concrete event dataclasses on replay. You ran the headline test: after opening, crediting, and debiting a ledger and saving it, a brand-new repository and aggregate reconstructed the correct balance purely by replaying the stored event stream — no stored balance column, no shared state.

You examined the `version` counter that the optimistic-concurrency guard uses: the store rejects any `append` whose `expected_version` does not match the stream's actual version, forcing the losing writer to retry from a fresh load. You wired `InMemorySnapshotStore` and confirmed the snapshot seam is harmless when the stream is shorter than the interval, and accelerating once it crosses the threshold.

You built `BalanceLedgerProjection` with `FunctionProjection` and `ProjectionRunner`, keeping a fast balance read model from the raw event stream — one that can be rebuilt from history at any time, unlike the bus-listener approach from Chapter 8. Finally, you connected the event store to the broker world via `TransactionalOutbox`, which enqueues events for at-least-once delivery and retries on failure so that no fact is silently lost between the store and downstream consumers. Chapter 10 picks up exactly there, introducing the Kafka and RabbitMQ adapters that the outbox relay sends events to.

---

## Try it yourself {.exercises}

1. **Replay to a point in time.** Apply ten credits of 100 cents each to a `LedgerAccount`. Then load the aggregate manually by calling `await store.load("led-X")`, filter envelopes to those with `sequence <= 5`, and replay only those through a fresh `LedgerAccount()`. Assert that the resulting balance equals 400 cents (four credits after the open) rather than 1 000 cents (ten credits). This is the "time-travel query" that state-storage models cannot provide.

2. **Implement a `Transferred` event and dual-aggregate save.** Add a `Transferred(DomainEvent)` with fields `source_id: str`, `target_id: str`, `amount: int`, `currency: str`. Add a `transfer_to(target: LedgerAccount, amount: Money) -> None` method to `LedgerAccount` that calls `self.debit(amount)` and `target.credit(amount)` in sequence, then applies a `Transferred` event on `self`. Wire a `demo_transfer` coroutine that opens two ledgers, credits 10 000 cents into the first, transfers 3 000 cents to the second, saves both aggregates independently, reloads both from the store, and asserts that the balances are 7 000 and 3 000 cents respectively.

3. **Add an `OwnerLedgerProjection`.** Write a second `FunctionProjection` named `owner_ledger` whose handler maintains a `dict[str, list[dict]]` mapping each `owner_id` to a chronological list of transaction records. Each record should include `event_type`, `amount` (from the payload for `Credited`/`Debited` events), and the envelope's sequence number. Feed it the same `InMemoryEventStore`, open three ledgers for the same owner, perform a mix of credits and debits, start the projection runner, and assert that the owner's transaction list has the correct number of entries in the correct order.
