<span class="eyebrow">Chapter 6</span>

# Domain-Driven Design {.chtitle}

::: figure art/openers/ch06.svg | &nbsp;

Lumen's wallet feature works. Deposits land, balances update, and the database persists everything across restarts. But look closely at Chapter 5's transfer service and you will notice something uncomfortable: the overdraft check lives in the service method, not in the wallet itself. The currency validation is a property comparison scattered across a handful of if-statements. Nothing stops a future developer — or a future you at 11 pm — from bypassing those guards by calling `repo.save(entity)` directly.

Domain-Driven Design addresses this by making the model responsible for its own rules. The data stops being a passive bag of values that any caller can mutate; it becomes an object with opinions — one that enforces its invariants, announces what happened, and cooperates with the persistence layer while staying free of any database import.

This chapter refactors the wallet into a proper DDD aggregate: a `Money` value object, a `Wallet` aggregate root that guards the overdraft rule and the currency-match rule, and a set of `DomainEvent`s emitted every time the wallet changes state. The `WalletEntity` from Chapter 5 stays exactly as it is — you will build a thin mapper that converts between the rich domain model and the flat persistence record.

---

## Entities and value objects

Before you can build a model that enforces its own rules, you need a vocabulary for the two fundamentally different kinds of objects that appear in every domain.

Think about what makes two wallets distinct. Even if two wallets happen to hold exactly one hundred euros, they are still separate wallets belonging to separate owners. You care *which one* you have. Now think about the amount itself. One hundred euros is one hundred euros — the exact Python object that holds that value is irrelevant; only the value matters. If a deposit adds fifty euros to a wallet's balance, you do not want to update the existing amount in place; you want to derive a brand-new amount that records the result. Mutating in place invites aliasing bugs where two parts of the code unknowingly share a reference to the same object and see each other's changes.

DDD names these two roles *entities* and *value objects*, and PyFly's `pyfly.domain` module makes them first-class concepts.

**`Entity[TID]`** tracks identity. Two instances are equal if and only if they share the same non-null `id`. Newly constructed entities with `id=None` are *transient* — they have not been persisted yet — and compare equal only by Python's object identity (`id()`). Hashing follows the same rule, so you can safely put entities in sets and dicts.

**`ValueObject`** tracks value. Subclass it with `@dataclass(frozen=True)` and Python's dataclass equality compares every field. The object is immutable by construction — any attempt to set an attribute raises `dataclasses.FrozenInstanceError`. The base class adds one convenience: a `replace(**changes)` helper that returns a new instance with the specified fields changed, the same idea as `dataclasses.replace` but available as a method.

Money is the textbook value object. An amount of one hundred euros is not a specific object you track over time; it is a value. Two separate `Money(100, "EUR")` instances are equal. A deposit does not mutate the existing amount — it produces a new one, leaving the original untouched and the model free of hidden side-effects.

Here is the `Money` value object for Lumen:

::: listing lumen/domain/money.py | Listing 6.1 — Money: an immutable value object with currency-aware arithmetic
from dataclasses import dataclass
from pyfly.domain import ValueObject


@dataclass(frozen=True, slots=True)
class Money(ValueObject):
    """An immutable amount in a specific currency."""

    amount: int      # stored in minor units (cents, pence, …)
    currency: str    # ISO 4217, e.g. "EUR", "USD"

    def add(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot add {self.currency} and {other.currency}"
            )
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise ValueError(
                f"Cannot subtract {other.currency} from {self.currency}"
            )
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def is_negative(self) -> bool:
        return self.amount < 0

    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        major = self.amount // 100
        minor = abs(self.amount) % 100
        return f"{major}.{minor:02d} {self.currency}"
:::

**How it works.** The amount is stored in minor units — integer cents, pence, or whatever the currency's smallest denomination is — to eliminate floating-point rounding entirely. Financial calculations that use `float` are a chronic source of off-by-one-cent bugs that only surface in production, usually during reconciliation. Storing 100 EUR as `amount=10000` keeps all arithmetic exact.

`add` and `subtract` each check currency compatibility before doing any arithmetic. Rejecting mismatched currencies at the operation level means the error surfaces exactly where the mistake was made, not somewhere downstream when the result gets compared to an expected total. Both methods return a *new* `Money` instance rather than modifying `self` — a direct consequence of the `frozen=True` decorator. This immutability guarantee means that the aggregate holding a `Money` value can never be partially updated: either the whole replacement succeeds or the old value is still in place, with nothing in between.

The `__str__` method converts back to a human-readable decimal for logging and responses, but the canonical representation in memory always stays as the integer.

!!! note "Minor units vs decimal"
    Storing money as integer cents is one convention; another is Python's `decimal.Decimal` with a fixed scale. Both are valid. What matters is picking one and sticking to it within the bounded context. For Lumen, integer cents keep the model free of import-time precision configuration.

!!! spring "Spring parity"
    `ValueObject` mirrors the `@ValueObject` / `@Embeddable` cluster in Spring's JPA ecosystem and the `ValueObject` marker interface from Spring Modulith. The `frozen=True` dataclass maps to Java's `record` type introduced in Java 16 — immutable, value-based equality, concise syntax. jMolecules's `@ValueObject` annotation carries the same intent.

---

## The aggregate root

`Money` solves the representation problem — amounts are now immutable and currency-aware. But Lumen still needs something to *own* the wallet's balance and decide when a deposit or withdrawal is permitted. That is the job of the aggregate root.

An entity becomes an *aggregate root* when it owns a cluster of related objects and acts as the single point of entry for all changes within that cluster. The aggregate root is the *consistency boundary*: no external code reaches inside and mutates an inner object directly. All changes go through the root's methods, which enforce the rules. This is the design that prevents the 11 pm bypass described in the chapter introduction — once every change must flow through the root, there is no back-channel.

`AggregateRoot[TID]` extends `Entity[TID]` with one addition: an internal buffer of *pending domain events*. Every state-changing method calls `self.raise_event(event)` to record what happened. When the repository saves the aggregate, it drains that buffer with `clear_events()` and hands the events to the application service, which publishes them to the event bus. You will see the full publish cycle in the Domain events section; for now, focus on the aggregate itself.

Here is the `Wallet` aggregate root:

::: listing lumen/domain/wallet.py | Listing 6.2 — Wallet: the aggregate root that owns balance and enforces its rules
from __future__ import annotations
import uuid
from dataclasses import dataclass

from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent

from lumen.domain.money import Money


# ── Domain events ─────────────────────────────────────────────────────────────

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
    new_balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    new_balance: int = 0


# ── Aggregate root ────────────────────────────────────────────────────────────

class Wallet(AggregateRoot[str]):
    """
    The Wallet aggregate root.

    All state changes happen through intent-revealing methods (deposit,
    withdraw).  Invariants are enforced here — callers never touch
    self._balance directly.
    """

    def __init__(
        self,
        id: str,
        owner_id: str,
        balance: Money,
    ) -> None:
        super().__init__(id)
        self._owner_id = owner_id
        self._balance = balance

    # ── Read-only accessors ────────────────────────────────────────────────

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def balance(self) -> Money:
        return self._balance

    # ── Factory method ─────────────────────────────────────────────────────

    @classmethod
    def open(cls, owner_id: str, currency: str) -> "Wallet":
        """Open a new wallet with a zero balance and emit WalletOpened."""
        wallet_id = str(uuid.uuid4())
        zero = Money(amount=0, currency=currency)
        wallet = cls(id=wallet_id, owner_id=owner_id, balance=zero)
        wallet.raise_event(
            WalletOpened(
                wallet_id=wallet_id,
                owner_id=owner_id,
                currency=currency,
            )
        )
        return wallet

    # ── Behaviour ──────────────────────────────────────────────────────────

    def deposit(self, amount: Money) -> None:
        """Credit the wallet.  Rejects currency mismatches."""
        if amount.currency != self._balance.currency:
            raise BusinessRuleViolation(
                rule="deposit-currency-mismatch",
                message=(
                    f"Cannot deposit {amount.currency} into a "
                    f"{self._balance.currency} wallet"
                ),
                context={"wallet_id": self.id},
            )
        if amount.is_negative() or amount.is_zero():
            raise BusinessRuleViolation(
                rule="deposit-amount-must-be-positive",
                message="Deposit amount must be greater than zero",
                context={"wallet_id": self.id},
            )
        self._balance = self._balance.add(amount)
        assert self.id is not None
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency,
                new_balance=self._balance.amount,
            )
        )

    def withdraw(self, amount: Money) -> None:
        """Debit the wallet.  Rejects overdrafts and currency mismatches."""
        if amount.currency != self._balance.currency:
            raise BusinessRuleViolation(
                rule="withdrawal-currency-mismatch",
                message=(
                    f"Cannot withdraw {amount.currency} from a "
                    f"{self._balance.currency} wallet"
                ),
                context={"wallet_id": self.id},
            )
        if amount.is_negative() or amount.is_zero():
            raise BusinessRuleViolation(
                rule="withdrawal-amount-must-be-positive",
                message="Withdrawal amount must be greater than zero",
                context={"wallet_id": self.id},
            )
        new_balance = self._balance.subtract(amount)
        if new_balance.is_negative():
            raise BusinessRuleViolation(
                rule="insufficient-funds",
                message="Withdrawal would overdraw the wallet",
                context={
                    "wallet_id": self.id,
                    "balance": self._balance.amount,
                    "requested": amount.amount,
                },
            )
        self._balance = new_balance
        assert self.id is not None
        self.raise_event(
            FundsWithdrawn(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency,
                new_balance=self._balance.amount,
            )
        )
:::

**How it works.** The aggregate boundary is enforced at three levels. First, `_balance` is a private attribute — Python's name-mangling convention signals clearly that it is not part of the public contract. Callers receive a read-only `balance` property and alter the wallet's state only through `deposit` or `withdraw`. Second, the factory class method `open` is the sole legitimate way to create a new wallet: it generates a UUID, initializes the zero balance in the wallet's native currency, and immediately queues the `WalletOpened` event. Using a factory method rather than calling `__init__` directly ensures that the opening event is *never* forgotten, even if a developer constructs a wallet deep inside a test fixture. Third, domain events such as `WalletOpened`, `FundsDeposited`, and `FundsWithdrawn` are frozen dataclasses defined at the top of the module — their fields are a deliberate record of the information that downstream consumers need to react. Notice that they capture `new_balance` as well as the transaction amount, so a subscriber does not need to call back into the aggregate to learn the current state after the change.

The diagram below shows the complete picture: state, invariants, and the events the wallet emits.

::: figure art/figures/06-aggregate.svg | Figure 6.1 — The Wallet aggregate: state, invariants, and the events it emits.

!!! spring "Spring parity"
    `AggregateRoot[str]` maps to jMolecules's `org.jmolecules.ddd.types.AggregateRoot<A, ID>` and to Spring Data's `AbstractAggregateRoot<A>`, which offers the same `registerEvent()` / `@DomainEvents` / `@AfterDomainEventPublication` mechanism. The pattern is identical in spirit: the aggregate accumulates events in a buffer; the repository drains them after a successful save; a `DomainEventPublisher` dispatches them. PyFly's `raise_event` + `clear_events` is the Python equivalent of `registerEvent` + `@AfterDomainEventPublication`.

---

## Protecting invariants

The aggregate root is only valuable if the rules it is supposed to enforce are actually unreachable by any other path. That is what the word *invariant* means in DDD: a condition the model must uphold regardless of how it is called, who calls it, or how many different services exist in the application. An invariant is not a suggestion — it is a constraint that cannot be violated because the model does not expose any mechanism to do so.

Lumen's `Wallet` has three invariants:

1. The balance must never go below zero (no overdraft).
2. Funds can only be deposited or withdrawn in the wallet's native currency.
3. Deposit and withdrawal amounts must be strictly positive.

All three are enforced inside the aggregate methods you just read. The framework's exception type for this is `BusinessRuleViolation` from `pyfly.domain`. It takes three arguments: a stable machine-readable `rule` slug (used in logs and error responses), a human-readable `message`, and an optional `context` dict that carries debugging fields like the wallet ID.

`BusinessRuleViolation` extends `pyfly.kernel.BusinessException`, which means the existing RFC 7807 problem-details mapper from Chapter 4's error handling translates it automatically into a structured HTTP 422 response — no extra handler required.

!!! warning "Keep invariants in the model, not the service"
    Moving the overdraft check back into `WalletService` creates two problems. First, any code that calls `repo.save(entity)` directly bypasses the check entirely. Second, you end up duplicating the rule across every path that modifies a wallet — the service, a background job, an admin command. When the rule changes — say, the product team introduces a configurable overdraft buffer — there is exactly one place to update: the aggregate method. That is the whole point.

The difference between a service-level guard and an aggregate invariant is enforceability. A service guard is a convention; an aggregate invariant is a physical constraint enforced by encapsulation. To make that difference concrete, here is what the service-level approach from Chapter 5 looked like, and why it is fragile:

::: listing lumen/wallet_service_before.py | Listing 6.3 — Before: business rules scattered across the service (fragile)
# DO NOT DO THIS — rules that belong in the model
from pyfly.container import service


@service
class WalletServiceBefore:

    async def withdraw(self, wallet_id: str, amount: float) -> None:
        # Rule lives here — but anyone calling repo.save directly skips it
        wallet = {"id": wallet_id, "balance": 50.0, "currency": "EUR"}
        if wallet["balance"] < amount:
            raise ValueError("Insufficient funds")
        wallet["balance"] -= amount
        # ... save
:::

And here is what the service looks like after the model takes ownership:

::: listing lumen/wallet_service_after.py | Listing 6.4 — After: the service delegates to the aggregate
from pyfly.container import service
from pyfly.domain import AggregateNotFound

from lumen.domain.money import Money


@service
class WalletService:

    async def withdraw(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: str,
    ) -> None:
        # The service orchestrates; the aggregate decides.
        wallet = await self._repo.find(wallet_id)
        if wallet is None:
            raise AggregateNotFound("Wallet", wallet_id)
        wallet.withdraw(Money(amount=amount_cents, currency=currency))
        await self._repo.save(wallet)
        # Events are drained and published by the repository/service boundary
:::

**How it works.** Notice how the after version communicates intent: `wallet.withdraw(...)` reads as "ask the wallet to withdraw". The service does not know — or care — what that entails. It trusts the aggregate to either succeed or raise a `BusinessRuleViolation`. This thin-orchestrator pattern has a practical consequence for team workflows: a new developer can implement a `transfer` endpoint without reading `WalletService` at all. The constraints are in `Wallet`, and that is the only place they need to look.

The `rule` slug on `BusinessRuleViolation` matters too. Strings like `"insufficient-funds"` and `"deposit-currency-mismatch"` travel in the RFC 7807 response body, where they can be matched by client code without parsing free-text messages. They also appear in structured log fields, making production alerts straightforward to write.

!!! note "AggregateNotFound"
    `AggregateNotFound` is the second domain exception in `pyfly.domain`. Raise it when the requested aggregate does not exist — it maps to a 404 problem-details response via the same RFC 7807 handler. The constructor takes the aggregate type name and the ID: `AggregateNotFound("Wallet", wallet_id)`.

---

## Domain events

Your aggregate now enforces its invariants and controls all state changes. But Lumen will eventually need to react to those changes: update an audit log, send a push notification, trigger fraud detection, publish a ledger entry. The naive solution is to put those side-effects directly inside `deposit` and `withdraw`. That couples the domain model to infrastructure — suddenly your wallet needs to know about Kafka topics and email templates, and every unit test drags in a broker connection.

Domain events are the solution. A domain event records something that *happened* inside the aggregate — past tense, immutable fact. The aggregate does not know what will be done with the fact; it only records it. Downstream consumers — event listeners, projectors, notification services — subscribe to the event and react in their own context, without the aggregate ever depending on them.

`DomainEvent` from `pyfly.domain` is a frozen-dataclass base that auto-populates three fields when an instance is created:

- `event_id` — a UUID v4 that uniquely identifies this occurrence.
- `occurred_at` — a UTC timestamp at the moment of construction.
- `event_type` — a property that defaults to the subclass's class name (`"WalletOpened"`, `"FundsDeposited"`, `"FundsWithdrawn"`).

You already saw the three wallet events defined in Listing 6.2. Here they are in isolation with an explicit look at what you get from the base:

::: listing lumen/domain/events.py | Listing 6.5 — Domain events and the fields DomainEvent provides automatically
from dataclasses import dataclass
from pyfly.domain import DomainEvent


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
    new_balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    new_balance: int = 0


# Each event carries event_id (UUID), occurred_at (UTC datetime),
# and event_type (class name) — all set by DomainEvent.__post_init__.

def demonstrate_event_fields() -> None:
    evt = FundsDeposited(
        wallet_id="w-1",
        amount=5000,
        currency="EUR",
        new_balance=15000,
    )
    print(evt.event_id)       # e.g. "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    print(evt.occurred_at)    # e.g. datetime(2026, 6, 7, 9, 30, 0, tzinfo=UTC)
    print(evt.event_type)     # "FundsDeposited"
:::

**How it works.** Each event class declares only the fields that are unique to that occurrence — `wallet_id`, `amount`, `currency`, `new_balance`. Everything else comes for free from `DomainEvent`: a UUID `event_id` ensures idempotent processing, `occurred_at` provides an audit timestamp, and `event_type` gives a name consumers can route on without inspecting the Python class. All fields default to zero or empty string so that the `frozen=True` dataclass machinery can still provide keyword-argument construction without requiring positional arguments.

Notice that `FundsDeposited` carries both `amount` (the transaction) and `new_balance` (the result). A subscriber that wants to update a read-model balance does not need to call back into the aggregate or the database — all the information it needs is in the event itself. That self-contained design makes event consumers simpler and avoids extra round-trips.

The event lifecycle spans two phases. First, inside the aggregate: when `wallet.deposit(amount)` succeeds, it calls `self.raise_event(FundsDeposited(...))`, appending the event to a private list maintained by `AggregateRoot`. Nothing is published yet — the aggregate is still in memory. Second, at the service boundary: after the repository saves the aggregate and the database transaction commits, the application service drains the buffer and publishes. This sequence — *save first, publish after* — guarantees that an event is never dispatched for a change that failed to persist. Listing 6.6 shows that boundary in full:

::: listing lumen/wallet_application_service.py | Listing 6.6 — Draining domain events after a successful save
from pyfly.container import service
from pyfly.eda import EventPublisher

from lumen.domain.money import Money
from lumen.domain.wallet import Wallet


@service
class WalletApplicationService:

    def __init__(
        self,
        repo: object,              # typed as WalletDomainRepository in practice
        events: EventPublisher,
    ) -> None:
        self._repo = repo
        self._events = events

    async def open_wallet(self, owner_id: str, currency: str) -> str:
        wallet = Wallet.open(owner_id=owner_id, currency=currency)
        await self._repo.save(wallet)
        for event in wallet.clear_events():
            await self._events.publish(event)
        assert wallet.id is not None
        return wallet.id

    async def deposit(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: str,
    ) -> None:
        wallet = await self._repo.find(wallet_id)
        wallet.deposit(Money(amount=amount_cents, currency=currency))
        await self._repo.save(wallet)
        for event in wallet.clear_events():
            await self._events.publish(event)
:::

**How it works.** `open_wallet` calls `Wallet.open`, which queues a `WalletOpened` event internally; after the `save`, `wallet.clear_events()` returns that one event, and `publish` dispatches it. The `deposit` method follows the same three-step pattern: load, mutate, save — then drain. The `for event in wallet.clear_events()` loop is intentionally explicit rather than hidden inside the repository, because the application service is the right place to decide *when* publishing happens (after the transaction boundary, not before).

!!! tip "Event ordering"
    `raise_event` appends to the buffer in call order. `clear_events` drains and clears it, returning events in the same order. If a single aggregate method raises multiple events (a batch operation, for example), they arrive at the event bus in the order they were raised — oldest first.

---

## Domain vs persistence

With the domain model and its events in place, there is one remaining tension to resolve: how does the `Wallet` aggregate reach the database?

The tempting shortcut is to annotate `Wallet` directly with SQLAlchemy `Mapped[]` fields and give it a `__tablename__`. That merges two concerns that change at very different rates: business rules change when the product evolves; column definitions change when the schema migrates. Mixing them means that a schema change forces you to touch the aggregate, and a rule change risks accidentally breaking a column mapping. It also drags SQLAlchemy into every unit test.

The alternative is two models that coexist without knowing about each other: `Wallet` as pure Python, `WalletEntity` as pure persistence. A mapper converts between them. This is not extra ceremony — it is the boundary that lets you test domain logic at full speed without a database and tune persistence independently.

`Wallet` (the aggregate root) is *pure Python*. It has no SQLAlchemy columns, no `Mapped[]` annotations, no `__tablename__`. You can instantiate it in a unit test with one line and exercise every invariant without a database connection.

`WalletEntity` (from Chapter 5) is *pure persistence*. It knows exactly how to map to the `wallets` table. It carries the five `BaseEntity` audit columns. It is what SQLAlchemy's session sees.

A **mapper** converts between the two. Keeping it in a dedicated module makes the boundary explicit and visible to anyone reading the codebase:

::: listing lumen/domain/wallet_mapper.py | Listing 6.7 — WalletMapper: converting between the domain aggregate and the persistence entity
from lumen.domain.money import Money
from lumen.domain.wallet import Wallet
from lumen.wallet_entity import WalletEntity


class WalletMapper:
    """Converts between the Wallet domain aggregate and WalletEntity."""

    @staticmethod
    def to_entity(wallet: Wallet) -> WalletEntity:
        """Produce a WalletEntity suitable for SQLAlchemy persistence."""
        entity = WalletEntity()
        entity.id = wallet.id  # type: ignore[assignment]
        entity.owner_id = wallet.owner_id
        entity.balance = float(wallet.balance.amount) / 100.0
        entity.currency = wallet.balance.currency
        return entity

    @staticmethod
    def to_domain(entity: WalletEntity) -> Wallet:
        """Reconstruct a Wallet aggregate from a persisted WalletEntity."""
        return Wallet(
            id=str(entity.id),
            owner_id=entity.owner_id,
            balance=Money(
                amount=round(float(entity.balance) * 100),
                currency=entity.currency,
            ),
        )
:::

**How it works.** `to_entity` converts the minor-unit integer amount back to a floating-point value for the database column (`/ 100.0`) — the inverse of the `round(float(...) * 100)` reconstruction in `to_domain`. These two conversions are the *only* place where the minor-unit / float boundary is crossed. A `# type: ignore[assignment]` comment acknowledges that `entity.id` is typed as `int | None` in the ORM layer while the domain uses `str`; in practice, UUID strings round-trip correctly via SQLAlchemy's `String` column.

The mapper is intentionally narrow. It does not enforce rules — `Wallet.__init__` and the behaviour methods do that. It does not publish events — the application service does that. It only translates shape, which means you can read it once and trust that it will never surprise you.

**The domain repository.** The repository that the application service works with speaks entirely in `Wallet` aggregates — it never exposes a `WalletEntity`. PyFly's `DomainRepository` protocol from `pyfly.domain` describes the contract — four small async methods — and the concrete implementation calls the mapper on every crossing. The application service never needs to import `WalletEntityRepository` or SQLAlchemy at all:

::: listing lumen/domain/wallet_repository.py | Listing 6.8 — WalletDomainRepository: a DomainRepository that maps to WalletEntity under the hood
from pyfly.container import repository
from pyfly.domain import AggregateNotFound, DomainRepository

from lumen.domain.wallet import Wallet
from lumen.domain.wallet_mapper import WalletMapper
from lumen.wallet_repository import WalletRepository as WalletEntityRepository


@repository
class WalletDomainRepository(DomainRepository[Wallet, str]):
    """
    Speaks Wallet aggregates to the application layer.
    Delegates to WalletEntityRepository for actual SQL.
    """

    def __init__(self, entity_repo: WalletEntityRepository) -> None:
        self._entity_repo = entity_repo

    async def find(self, wallet_id: str) -> Wallet | None:
        entity = await self._entity_repo.find_by_id(wallet_id)
        if entity is None:
            return None
        return WalletMapper.to_domain(entity)

    async def save(self, wallet: Wallet) -> None:
        entity = WalletMapper.to_entity(wallet)
        await self._entity_repo.save(entity)

    async def add(self, wallet: Wallet) -> None:
        await self.save(wallet)

    async def remove(self, wallet_id: str) -> None:
        await self._entity_repo.delete(wallet_id)

    async def next_id(self) -> str:
        import uuid
        return str(uuid.uuid4())
:::

**How it works.** `WalletDomainRepository` is decorated with `@repository`, which registers it in the IoC container and makes it available for injection. Its constructor receives a `WalletEntityRepository` — the standard Spring-style entity repo from Chapter 5 — and stores it as `_entity_repo`. Every method is a one-liner: `find` asks the entity repo for a row, short-circuits to `None` if absent, and otherwise calls `WalletMapper.to_domain`; `save` does the inverse. The `add` alias delegates to `save` for callers that prefer the semantics of *inserting* rather than *upserting*. `next_id` generates a fresh UUID for callers that need an ID before the aggregate is saved — useful in the CQRS pattern you will see in Chapter 7.

This is the hexagonal architecture Figure 5.1 described in its ports-and-adapters form: the application layer depends on the domain repository port; the adapter depends on both the port and the SQLAlchemy `WalletEntityRepository`. No domain code ever sees a SQLAlchemy type.

!!! spring "Spring parity"
    This double-layer repository is the Python equivalent of the pattern advocated in Vaughn Vernon's *Implementing Domain-Driven Design* for Spring: a `WalletRepository` interface (domain port), a `WalletJpaRepository` (Spring Data JPA), and a `WalletRepositoryImpl` that calls the JPA repository and maps between `Wallet` aggregate and `WalletJpaEntity`. The mapper corresponds to MapStruct's generated code in that world, or a hand-written `WalletAssembler`. The structure is identical; the boilerplate is less.

---

## Specifications for business rules

The aggregate guards state-changing operations extremely well — you cannot overdraw a wallet or deposit the wrong currency. But not every rule is about mutation. Some rules are *eligibility checks*: "before we show this user the withdrawal button, is the wallet in a state that makes the operation meaningful?" or "out of ten thousand wallets, which ones qualify for the loyalty bonus?" These are read-only predicates, and encoding them as aggregate methods would clutter `Wallet` with query logic that has nothing to do with state transitions.

The Specification pattern solves this cleanly. A specification is a named, reusable predicate: a single `is_satisfied_by(obj) -> bool` method wrapped in an object that can be composed with other specifications using Boolean operators. Because each rule is its own class, you can name rules clearly, reuse them across services, and combine them at runtime based on the calling context — something an `if` chain cannot do.

`Specification[T]` from `pyfly.domain` is a composable predicate for in-memory objects. Subclass it, implement `is_satisfied_by`, and combine instances with `&` (and), `|` (or), and `~` (not). A specification is also directly callable, so you can pass it to Python's built-in `filter` without any adapter code.

!!! note "Two kinds of specification"
    `pyfly.domain.Specification` is the in-memory predicate used inside domain services. `pyfly.data.relational.sqlalchemy.Specification` (Chapter 5) is the database-aware query predicate that pushes the rule down into SQL. The two coexist. Domain specifications are for business logic; data specifications are for queries.

Here is a specification that expresses the "eligible for withdrawal" rule:

::: listing lumen/domain/specs.py | Listing 6.9 — EligibleForWithdrawal: a composable domain Specification
from pyfly.domain import Specification

from lumen.domain.wallet import Wallet


class HasPositiveBalance(Specification[Wallet]):
    """The wallet has at least one cent remaining."""

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return not wallet.balance.is_zero() and not wallet.balance.is_negative()


class IsInCurrency(Specification[Wallet]):
    """The wallet holds a specific currency."""

    def __init__(self, currency: str) -> None:
        self._currency = currency

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return wallet.balance.currency == self._currency


# Compose: a wallet is eligible for withdrawal if it has a positive
# balance in the requested currency.
def eligible_for_withdrawal(currency: str) -> Specification[Wallet]:
    return HasPositiveBalance() & IsInCurrency(currency)


# Use as a predicate:
def filter_eligible(
    wallets: list[Wallet],
    currency: str,
) -> list[Wallet]:
    spec = eligible_for_withdrawal(currency)
    return list(filter(spec, wallets))
:::

**How it works.** `HasPositiveBalance` and `IsInCurrency` are each a single method — the concrete rule, nothing else. The `eligible_for_withdrawal` factory combines them with `&`, producing a composite specification whose `is_satisfied_by` returns `True` only when both component checks pass. Because `Specification` implements `__call__`, you can pass the composite directly to `filter()` without a lambda wrapper.

The key design discipline: a specification is a *predicate*, not a *guard*. It returns `True` or `False` and never raises an exception. Aggregate invariants (overdraft, currency mismatch) belong inside `deposit` and `withdraw` because they must prevent the state change from occurring. Specifications belong in services and query handlers because they *select* or *classify* — they do not protect.

Specifications shine in domain services and read-model queries where you need to combine rules dynamically — for example, an admin search that adds filters depending on the operator's role, or a batch job that iterates a list and partitions it into eligible and ineligible wallets. They are also straightforward to unit-test in isolation, since each class has exactly one method and no side-effects.

!!! tip "Specification.of for quick lambdas"
    For one-off predicates that do not need a class, use the factory method: `spec = Specification.of(lambda w: w.balance.amount >= 1000, name="minimum-balance")`. It composes with `&`, `|`, and `~` the same way as a full subclass.

---

## What you built {.recap}

Lumen's wallet is now a first-class domain model.

You introduced `Money`, a frozen `ValueObject` that stores amounts in minor units, enforces currency homogeneity at the arithmetic level, and is replaced rather than mutated. You promoted the wallet itself to a `Wallet(AggregateRoot[str])` with read-only properties and intent-revealing behaviour methods (`open`, `deposit`, `withdraw`) that enforce all three invariants — no overdraft, no cross-currency operations, no zero-or-negative amounts — by raising `BusinessRuleViolation` with a stable rule slug, a human message, and a context dict that the RFC 7807 mapper turns into a structured 422 response automatically.

Inside every state-changing method you called `raise_event`, queuing `WalletOpened`, `FundsDeposited`, or `FundsWithdrawn` — frozen `DomainEvent` subclasses that carry their own `event_id` and `occurred_at`. After a successful repository save, the application service drains those events with `clear_events()` and hands them to `EventPublisher`. The persistence layer sees only `WalletEntity` and the five `BaseEntity` audit columns; a `WalletMapper` translates between the two shapes, keeping both models clean. `WalletDomainRepository` wraps the SQLAlchemy repository and speaks aggregates to the application layer. Finally, `Specification[Wallet]` gave you a composable, callable predicate for business-rule queries that live outside the aggregate boundary.

The controller is untouched. The service shrank. The rules are now enforced by the object that owns them.

---

## Try it yourself {.exercises}

1. **Add a `transfer_to` method and a `DailyLimit` value object.** Add a `DailyLimit(ValueObject)` with `max_amount: int` and `currency: str`, decorated with `@dataclass(frozen=True, slots=True)`. Then add `Wallet.transfer_to(target: Wallet, amount: Money) -> None`. The method should call `self.withdraw(amount)` and `target.deposit(amount)` in sequence, raising `BusinessRuleViolation(rule="transfer-currency-mismatch")` if the two wallets hold different currencies. Verify that both wallets each accumulate a `FundsWithdrawn` / `FundsDeposited` event, respectively, and that a transfer between mismatched wallets raises before modifying either balance.

2. **Add a `WalletFrozen` event and a `freeze()` behaviour.** Define a `WalletFrozen(DomainEvent)` with `wallet_id: str = ""` and `reason: str = ""`. Add a `_frozen: bool` field to `Wallet.__init__` (default `False`). Add a `freeze(reason: str) -> None` method that sets `_frozen = True` and raises `WalletOpened` — wait, `WalletFrozen`. Guard `deposit` and `withdraw` with `if self._frozen: raise BusinessRuleViolation(rule="wallet-frozen")` at the top of each method. Then write an event listener using `@event_listener` from `pyfly.eda` that logs a structured warning every time a `WalletFrozen` event is published.

3. **Express a business rule as a Specification.** Write a `MinimumBalance(Specification[Wallet])` that checks whether a wallet's balance is at or above a threshold amount (in minor units) passed to its `__init__`. Combine it with `IsInCurrency` from Listing 6.9 using `&` to produce a `premium_eligible(currency, threshold)` factory function. Call `list(filter(premium_eligible("EUR", 50000), wallets))` over a list of test wallets and assert that only wallets with at least 500.00 EUR (50 000 cents) appear in the result.
