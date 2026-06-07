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

::: listing lumen/models/entities/v1/money.py | Listing 6.1 — Money: an immutable value object with currency-aware arithmetic
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.domain import BusinessRuleViolation, ValueObject


@dataclass(frozen=True)
class Money(ValueObject):
    """An exact monetary amount in a single currency.

    ``amount`` is in minor units (e.g. cents): ``Money(1050,
    Currency.EUR)`` is €10.50. Arithmetic returns new ``Money``
    instances and refuses to mix currencies.
    """

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise BusinessRuleViolation(
                "money-amount-integer",
                "amount must be an integer number of minor units",
            )

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """The additive identity for *currency* (a zero balance)."""
        return cls(amount=0, currency=currency)

    def add(self, other: Money) -> Money:
        """Return ``self + other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: Money) -> Money:
        """Return ``self - other``; both must share a currency."""
        self._assert_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    @property
    def major_units(self) -> float:
        """The amount rendered as a major-unit decimal (cents / 100)."""
        return round(self.amount / 100, 2)

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise BusinessRuleViolation(
                "money-currency-mismatch",
                f"cannot combine {self.currency.value} "
                f"with {other.currency.value}",
            )

    def __str__(self) -> str:
        return f"{self.major_units:.2f} {self.currency.value}"
:::

**How it works.** The amount is stored in minor units — integer cents, pence, or whatever the currency's smallest denomination is — to eliminate floating-point rounding entirely. Financial calculations that use `float` are a chronic source of off-by-one-cent bugs that only surface in production, usually during reconciliation. Storing €10.50 as `amount=1050` keeps all arithmetic exact. `__post_init__` rejects non-integer amounts immediately by raising `BusinessRuleViolation("money-amount-integer")`, so a stray float like `10.5` never silently enters the model.

The `currency` field uses the `Currency` enum (`Currency.EUR`, `Currency.USD`, `Currency.GBP`) rather than a bare string. This rules out typos at construction time and makes currency comparisons use Python's identity check (`is`) inside `_assert_same_currency` — which raises `BusinessRuleViolation("money-currency-mismatch")` if the currencies differ. `add` and `subtract` delegate that check before performing arithmetic, so the error surfaces exactly where the mistake was made.

Both methods return a *new* `Money` instance rather than modifying `self` — a direct consequence of the `frozen=True` decorator. This immutability guarantee means that the aggregate holding a `Money` value can never be partially updated: either the whole replacement succeeds or the old value is still in place, with nothing in between. The `is_positive` and `is_negative` properties (not methods — no call parentheses needed) expose the sign without exposing the raw integer. `major_units` converts back to a decimal for display, and `__str__` formats to `"10.50 EUR"` using `currency.value`.

!!! note "Minor units vs decimal"
    Storing money as integer cents is one convention; another is Python's `decimal.Decimal` with a fixed scale. Both are valid. What matters is picking one and sticking to it within the bounded context. For Lumen, integer minor units keep the model free of import-time precision configuration, and `__post_init__` enforces the constraint with `BusinessRuleViolation("money-amount-integer")` so a float never silently enters the model.

!!! spring "Spring parity"
    `ValueObject` mirrors the `@ValueObject` / `@Embeddable` cluster in Spring's JPA ecosystem and the `ValueObject` marker interface from Spring Modulith. The `frozen=True` dataclass maps to Java's `record` type introduced in Java 16 — immutable, value-based equality, concise syntax. jMolecules's `@ValueObject` annotation carries the same intent.

---

## The aggregate root

`Money` solves the representation problem — amounts are now immutable and currency-aware. But Lumen still needs something to *own* the wallet's balance and decide when a deposit or withdrawal is permitted. That is the job of the aggregate root.

An entity becomes an *aggregate root* when it owns a cluster of related objects and acts as the single point of entry for all changes within that cluster. The aggregate root is the *consistency boundary*: no external code reaches inside and mutates an inner object directly. All changes go through the root's methods, which enforce the rules. This is the design that prevents the 11 pm bypass described in the chapter introduction — once every change must flow through the root, there is no back-channel.

`AggregateRoot[TID]` extends `Entity[TID]` with one addition: an internal buffer of *pending domain events*. Every state-changing method calls `self.raise_event(event)` to record what happened. When the repository saves the aggregate, it drains that buffer with `clear_events()` and hands the events to the application service, which publishes them to the event bus. You will see the full publish cycle in the Domain events section; for now, focus on the aggregate itself.

Here is the `Wallet` aggregate root:

::: listing lumen/models/entities/v1/wallet_entity.py | Listing 6.2 — Wallet: the aggregate root that owns balance and enforces its rules
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from pyfly.domain import AggregateRoot, BusinessRuleViolation, DomainEvent


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
    balance: int = 0


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0


# ── Aggregate root ────────────────────────────────────────────────────────────

class Wallet(AggregateRoot[str]):
    """Wallet aggregate root — owns the ``balance >= 0`` invariant."""

    __slots__ = ("owner_id", "balance", "created_at")

    def __init__(
        self,
        id: str,
        owner_id: str,
        balance: Money,
        created_at: datetime | None = None,
    ) -> None:
        super().__init__(id)
        self.owner_id = owner_id
        self.balance = balance
        self.created_at = created_at or datetime.now(UTC)

    @property
    def currency(self) -> Currency:
        return self.balance.currency

    # ── Factory method ─────────────────────────────────────────────────────

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: Currency) -> Wallet:
        """Open a new, empty wallet; raises WalletOpened."""
        if not owner_id.strip():
            raise BusinessRuleViolation(
                "wallet-owner-required", "owner_id is required"
            )
        wallet = cls(
            id=wallet_id,
            owner_id=owner_id,
            balance=Money.zero(currency),
        )
        wallet.raise_event(
            WalletOpened(
                wallet_id=wallet_id,
                owner_id=owner_id,
                currency=currency.value,
            )
        )
        return wallet

    # ── Behaviour ──────────────────────────────────────────────────────────

    def deposit(self, amount: Money) -> None:
        """Credit *amount* to the balance; raises FundsDeposited."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "wallet-deposit-positive",
                "deposit amount must be > 0",
            )
        self.balance = self.balance.add(amount)
        assert self.id is not None
        self.raise_event(
            FundsDeposited(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    def withdraw(self, amount: Money) -> None:
        """Debit *amount*; refuses to overdraw. Raises FundsWithdrawn."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation(
                "wallet-withdrawal-positive",
                "withdrawal amount must be > 0",
            )
        remaining = self.balance.subtract(amount)
        if remaining.is_negative:
            raise BusinessRuleViolation(
                "wallet-insufficient-funds",
                f"cannot withdraw {amount}; balance is {self.balance}",
            )
        self.balance = remaining
        assert self.id is not None
        self.raise_event(
            FundsWithdrawn(
                wallet_id=self.id,
                amount=amount.amount,
                currency=amount.currency.value,
                balance=self.balance.amount,
            )
        )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.balance.currency:
            raise BusinessRuleViolation(
                "wallet-currency-mismatch",
                f"wallet holds {self.balance.currency.value}, "
                f"got {amount.currency.value}",
            )
:::

**How it works.** The aggregate boundary is enforced at three levels. First, `__slots__` locks the set of attributes at the class level, and `balance` and `owner_id` are public-but-deliberately-structured: only the aggregate's own methods (`deposit`, `withdraw`) mutate them, while the `currency` property is a read-only convenience that delegates to `balance.currency`. Second, the factory classmethod `open` is the sole legitimate way to create a new wallet: the caller supplies the `wallet_id` (so the application layer controls ID generation), `open` validates that `owner_id` is non-blank, initializes the balance with `Money.zero(currency)`, and immediately queues `WalletOpened`. Using a factory rather than calling `__init__` directly ensures the opening event is *never* forgotten, even in a test fixture. Third, domain events — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are frozen dataclasses defined at the top of the module. Notice that `FundsDeposited` and `FundsWithdrawn` carry a `balance` field (the balance *after* the operation, in minor units), so a subscriber never needs to call back into the aggregate to learn the current state after the change.

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

All three are enforced inside the aggregate methods you just read. The framework's exception type for this is `BusinessRuleViolation` from `pyfly.domain`. It takes two required arguments: a stable machine-readable `rule` slug (used in logs and error responses) and a human-readable `message`. The rule slugs Lumen uses are `"wallet-insufficient-funds"`, `"wallet-currency-mismatch"`, `"wallet-deposit-positive"`, and `"wallet-withdrawal-positive"` — each a compact, kebab-cased label that travels in the RFC 7807 response body and in structured log fields.

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

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money


@service
class WalletService:

    async def withdraw(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: Currency,
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

The `rule` slug on `BusinessRuleViolation` matters too. Strings like `"wallet-insufficient-funds"` and `"wallet-currency-mismatch"` travel in the RFC 7807 response body, where they can be matched by client code without parsing free-text messages. They also appear in structured log fields, making production alerts straightforward to write.

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

::: listing lumen/models/entities/v1/wallet_entity.py | Listing 6.5 — Domain events and the fields DomainEvent provides automatically
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
    balance: int = 0   # balance after the deposit, in minor units


@dataclass(frozen=True)
class FundsWithdrawn(DomainEvent):
    wallet_id: str = ""
    amount: int = 0
    currency: str = ""
    balance: int = 0   # balance after the withdrawal, in minor units


# Each event carries event_id (UUID), occurred_at (UTC datetime),
# and event_type (class name) — all set by DomainEvent.__post_init__.

def demonstrate_event_fields() -> None:
    evt = FundsDeposited(
        wallet_id="w-1",
        amount=5000,
        currency="EUR",
        balance=15000,
    )
    print(evt.event_id)       # e.g. "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    print(evt.occurred_at)    # e.g. datetime(2026, 6, 7, 9, 30, 0, tzinfo=UTC)
    print(evt.event_type)     # "FundsDeposited"
:::

**How it works.** Each event class declares only the fields that are unique to that occurrence — `wallet_id`, `amount`, `currency`, `new_balance`. Everything else comes for free from `DomainEvent`: a UUID `event_id` ensures idempotent processing, `occurred_at` provides an audit timestamp, and `event_type` gives a name consumers can route on without inspecting the Python class. All fields default to zero or empty string so that the `frozen=True` dataclass machinery can still provide keyword-argument construction without requiring positional arguments.

Notice that `FundsDeposited` carries both `amount` (the transaction) and `balance` (the balance after the operation, in minor units). A subscriber that wants to update a read-model balance does not need to call back into the aggregate or the database — all the information it needs is in the event itself. That self-contained design makes event consumers simpler and avoids extra round-trips.

The event lifecycle spans two phases. First, inside the aggregate: when `wallet.deposit(amount)` succeeds, it calls `self.raise_event(FundsDeposited(...))`, appending the event to a private list maintained by `AggregateRoot`. Nothing is published yet — the aggregate is still in memory. Second, at the service boundary: after the repository saves the aggregate and the database transaction commits, the application service drains the buffer and publishes. This sequence — *save first, publish after* — guarantees that an event is never dispatched for a change that failed to persist. Listing 6.6 shows that boundary in full:

::: listing lumen/wallet_application_service.py | Listing 6.6 — Draining domain events after a successful save
import uuid

from pyfly.container import service
from pyfly.eda import EventPublisher

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet


@service
class WalletApplicationService:

    def __init__(
        self,
        repo: object,              # typed as WalletDomainRepository in practice
        events: EventPublisher,
    ) -> None:
        self._repo = repo
        self._events = events

    async def open_wallet(self, owner_id: str, currency: Currency) -> str:
        wallet_id = str(uuid.uuid4())
        wallet = Wallet.open(
            wallet_id=wallet_id, owner_id=owner_id, currency=currency
        )
        await self._repo.save(wallet)
        for event in wallet.clear_events():
            await self._events.publish(event)
        return wallet_id

    async def deposit(
        self,
        wallet_id: str,
        amount_cents: int,
        currency: Currency,
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

The alternative is two models that coexist without knowing about each other: `Wallet` as pure Python, `WalletRow` as pure persistence. The SQLAlchemy adapter converts between them. This is not extra ceremony — it is the boundary that lets you test domain logic at full speed without a database and tune persistence independently.

`Wallet` (the aggregate root) is *pure Python*. It has no SQLAlchemy columns, no `Mapped[]` annotations, no `__tablename__`. You can instantiate it in a unit test with two lines and exercise every invariant without a database connection.

`WalletRow` is *pure persistence*. It carries five columns — `id`, `owner_id`, `currency`, `balance_minor` (integer minor units), and `created_at` — and knows nothing about domain invariants or events. It is what SQLAlchemy's session sees.

The SQLAlchemy adapter converts between the two. Listing 6.7 shows the key translation in a standalone mapper class form to make the pattern explicit; in the real `SqlAlchemyWalletRepository` the equivalent conversion lives inside the `_to_aggregate` helper and the `add` method:

::: listing lumen/domain/wallet_mapper.py | Listing 6.7 — WalletMapper: converting between the domain aggregate and the persistence row
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet


class WalletRow:
    """Simplified stand-in for the SQLAlchemy WalletRow (see sql_wallet_repository.py)."""
    id: str
    owner_id: str
    currency: str
    balance_minor: int


class WalletMapper:
    """Converts between the Wallet domain aggregate and WalletRow."""

    @staticmethod
    def to_row(wallet: Wallet) -> WalletRow:
        """Produce a WalletRow suitable for SQLAlchemy persistence."""
        row = WalletRow()
        row.id = wallet.id  # type: ignore[assignment]
        row.owner_id = wallet.owner_id
        row.currency = wallet.balance.currency.value
        row.balance_minor = wallet.balance.amount  # integer minor units
        return row

    @staticmethod
    def to_domain(row: WalletRow) -> Wallet:
        """Reconstruct a Wallet aggregate from a persisted WalletRow."""
        currency = Currency(row.currency)
        return Wallet(
            id=row.id,
            owner_id=row.owner_id,
            balance=Money(amount=row.balance_minor, currency=currency),
        )
:::

**How it works.** `to_row` writes `wallet.balance.amount` (an integer) directly into the `balance_minor` column — no float conversion needed. `to_domain` reconstructs a `Currency` enum from the stored ISO string via `Currency(row.currency)`, then builds a `Money` with the raw integer minor-unit value. There is no floating-point boundary crossing anywhere in the mapping layer: the database column stores the integer, the domain object holds the integer, and the mapper simply passes it through. The real SQLAlchemy adapter (`SqlAlchemyWalletRepository` in `sql_wallet_repository.py`) embeds this mapping directly inside its `add` and `find` methods rather than using a separate mapper class — both approaches express the same idea.

The mapper is intentionally narrow. It does not enforce rules — `Wallet.__init__` and the behaviour methods do that. It does not publish events — the application service does that. It only translates shape, which means you can read it once and trust that it will never surprise you.

**The domain repository.** The repository that the application service works with speaks entirely in `Wallet` aggregates — it never exposes a `WalletRow`. PyFly's `DomainRepository` protocol from `pyfly.domain` describes the contract — a handful of async methods — and the concrete implementation converts between aggregate and row on every crossing. The application service never needs to import `SqlAlchemyWalletRepository` or SQLAlchemy at all:

::: listing lumen/domain/wallet_repository.py | Listing 6.8 — WalletDomainRepository: a DomainRepository that maps to WalletRow under the hood
import uuid

from pyfly.container import repository
from pyfly.domain import DomainRepository

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet


@repository
class WalletDomainRepository(DomainRepository[Wallet, str]):
    """
    Speaks Wallet aggregates to the application layer.
    Delegates to SqlAlchemyWalletRepository for actual SQL.
    """

    def __init__(self, sql_repo: object) -> None:  # SqlAlchemyWalletRepository
        self._sql_repo = sql_repo

    async def find(self, wallet_id: str) -> Wallet | None:
        return await self._sql_repo.find(wallet_id)  # already maps to Wallet

    async def save(self, wallet: Wallet) -> Wallet:
        return await self._sql_repo.add(wallet)

    async def add(self, wallet: Wallet) -> Wallet:
        return await self._sql_repo.add(wallet)

    async def remove(self, wallet: Wallet) -> None:
        await self._sql_repo.remove(wallet)

    async def next_id(self) -> str:
        return await self._sql_repo.next_id()
:::

**How it works.** `WalletDomainRepository` is decorated with `@repository`, which registers it in the IoC container and makes it available for injection. Its constructor receives a `SqlAlchemyWalletRepository`, which already speaks `Wallet` aggregates (the row-to-aggregate conversion lives inside the SQL adapter's `_to_aggregate` helper). Every domain-repo method delegates in a single call. `find` asks the SQL repo and gets a `Wallet | None` back; `add` and `save` both call `sql_repo.add` which upserts the row; `remove` passes the aggregate through; `next_id` delegates to the SQL repo's UUID generator. The `add` alias exists so callers that think in insert semantics can express that intent without caring whether the underlying store is upsert-based.

This is the hexagonal architecture Figure 5.1 described in its ports-and-adapters form: the application layer depends on the domain repository port; the adapter depends on both the port and the SQLAlchemy `WalletEntityRepository`. No domain code ever sees a SQLAlchemy type.

!!! spring "Spring parity"
    This double-layer repository is the Python equivalent of the pattern advocated in Vaughn Vernon's *Implementing Domain-Driven Design* for Spring: a `WalletRepository` interface (domain port), a `WalletJpaRepository` (Spring Data JPA), and a `WalletRepositoryImpl` that calls the JPA repository and maps between `Wallet` aggregate and `WalletJpaEntity`. The row-to-aggregate conversion inside `SqlAlchemyWalletRepository._to_aggregate` corresponds to MapStruct's generated code or a hand-written `WalletAssembler` in that world. The structure is identical; the boilerplate is less.

---

## Specifications for business rules

The aggregate guards state-changing operations extremely well — you cannot overdraw a wallet or deposit the wrong currency. But not every rule is about mutation. Some rules are *eligibility checks*: "before we show this user the withdrawal button, is the wallet in a state that makes the operation meaningful?" or "out of ten thousand wallets, which ones qualify for the loyalty bonus?" These are read-only predicates, and encoding them as aggregate methods would clutter `Wallet` with query logic that has nothing to do with state transitions.

The Specification pattern solves this cleanly. A specification is a named, reusable predicate: a single `is_satisfied_by(obj) -> bool` method wrapped in an object that can be composed with other specifications using Boolean operators. Because each rule is its own class, you can name rules clearly, reuse them across services, and combine them at runtime based on the calling context — something an `if` chain cannot do.

`Specification[T]` from `pyfly.domain` is a composable predicate for in-memory objects. Subclass it, implement `is_satisfied_by`, and combine instances with `&` (and), `|` (or), and `~` (not). A specification is also directly callable, so you can pass it to Python's built-in `filter` without any adapter code.

!!! note "Two kinds of specification"
    `pyfly.domain.Specification` is the in-memory predicate used inside domain services. `pyfly.data.relational.sqlalchemy.Specification` (Chapter 5) is the database-aware query predicate that pushes the rule down into SQL. The two coexist. Domain specifications are for business logic; data specifications are for queries.

Here is a specification that expresses the "eligible for withdrawal" rule:

::: listing lumen/domain/specs.py | Listing 6.9 — EligibleForWithdrawal: a composable domain Specification
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.wallet_entity import Wallet
from pyfly.domain import Specification


class HasPositiveBalance(Specification[Wallet]):
    """The wallet has at least one cent remaining."""

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return wallet.balance.is_positive


class IsInCurrency(Specification[Wallet]):
    """The wallet holds a specific currency."""

    def __init__(self, currency: Currency) -> None:
        self._currency = currency

    def is_satisfied_by(self, wallet: Wallet) -> bool:
        return wallet.balance.currency is self._currency


# Compose: a wallet is eligible for withdrawal if it has a positive
# balance in the requested currency.
def eligible_for_withdrawal(currency: Currency) -> Specification[Wallet]:
    return HasPositiveBalance() & IsInCurrency(currency)


# Use as a predicate:
def filter_eligible(
    wallets: list[Wallet],
    currency: Currency,
) -> list[Wallet]:
    spec = eligible_for_withdrawal(currency)
    return list(filter(spec, wallets))
:::

**How it works.** `HasPositiveBalance` and `IsInCurrency` are each a single method — the concrete rule, nothing else. `HasPositiveBalance` delegates to `wallet.balance.is_positive` (a property, no parentheses), and `IsInCurrency` uses identity comparison (`is`) because `Currency` is a `StrEnum` with singleton members. The `eligible_for_withdrawal` factory combines them with `&`, producing a composite specification whose `is_satisfied_by` returns `True` only when both component checks pass. Because `Specification` implements `__call__`, you can pass the composite directly to `filter()` without a lambda wrapper.

The key design discipline: a specification is a *predicate*, not a *guard*. It returns `True` or `False` and never raises an exception. Aggregate invariants (overdraft, currency mismatch) belong inside `deposit` and `withdraw` because they must prevent the state change from occurring. Specifications belong in services and query handlers because they *select* or *classify* — they do not protect.

Specifications shine in domain services and read-model queries where you need to combine rules dynamically — for example, an admin search that adds filters depending on the operator's role, or a batch job that iterates a list and partitions it into eligible and ineligible wallets. They are also straightforward to unit-test in isolation, since each class has exactly one method and no side-effects.

!!! tip "Specification.of for quick lambdas"
    For one-off predicates that do not need a class, use the factory method: `spec = Specification.of(lambda w: w.balance.amount >= 1000, name="minimum-balance")`. It composes with `&`, `|`, and `~` the same way as a full subclass.

---

## What you built {.recap}

Lumen's wallet is now a first-class domain model.

You introduced `Money`, a frozen `ValueObject` that stores amounts in integer minor units plus a `Currency` enum, enforces currency homogeneity via `_assert_same_currency` (raising `BusinessRuleViolation("money-currency-mismatch")`), and is replaced rather than mutated. `__post_init__` rejects float amounts immediately. `is_positive` and `is_negative` are properties, not methods; `major_units` and `__str__` handle display.

You promoted the wallet itself to a `Wallet(AggregateRoot[str])` with `__slots__`, public-but-owned attributes (`balance`, `owner_id`), and a `currency` convenience property. Its intent-revealing factory and behaviour methods (`open`, `deposit`, `withdraw`) enforce all three invariants — no overdraft (`wallet-insufficient-funds`), no cross-currency operations (`wallet-currency-mismatch`), no non-positive amounts (`wallet-deposit-positive`, `wallet-withdrawal-positive`) — by raising `BusinessRuleViolation` with a stable rule slug and a human message. The factory `open` requires the caller to supply the `wallet_id` and a non-blank `owner_id`.

Inside every state-changing method you called `raise_event`, queuing `WalletOpened`, `FundsDeposited`, or `FundsWithdrawn` — frozen `DomainEvent` subclasses that carry their own `event_id` and `occurred_at`. `FundsDeposited` and `FundsWithdrawn` record the post-operation balance in the `balance` field (in minor units). After a successful repository save, the application service drains those events with `clear_events()` and hands them to `EventPublisher`. The persistence layer sees only `WalletRow` with five columns (`id`, `owner_id`, `currency`, `balance_minor`, `created_at`); `SqlAlchemyWalletRepository._to_aggregate` translates the row back into a `Wallet`, keeping both models clean. `WalletDomainRepository` wraps the SQLAlchemy adapter and speaks aggregates to the application layer. Finally, `Specification[Wallet]` gave you a composable, callable predicate for business-rule queries that live outside the aggregate boundary.

The controller is untouched. The service shrank. The rules are now enforced by the object that owns them.

---

## Try it yourself {.exercises}

1. **Add a `transfer_to` method and a `DailyLimit` value object.** Add a `DailyLimit(ValueObject)` with `max_amount: int` and `currency: Currency`, decorated with `@dataclass(frozen=True)`. Then add `Wallet.transfer_to(target: Wallet, amount: Money) -> None`. The method should call `self.withdraw(amount)` and `target.deposit(amount)` in sequence, raising `BusinessRuleViolation("transfer-currency-mismatch", ...)` if the two wallets hold different currencies. Because `Wallet` uses `__slots__`, add `"_frozen"` to the tuple if you also do exercise 2. Verify that both wallets each accumulate a `FundsWithdrawn` / `FundsDeposited` event, respectively, and that a transfer between mismatched wallets raises before modifying either balance.

2. **Add a `WalletFrozen` event and a `freeze()` behaviour.** Define `WalletFrozen(DomainEvent)` with `wallet_id: str = ""` and `reason: str = ""`. Add `"frozen"` to `__slots__` and a `frozen: bool` attribute (default `False`) to `Wallet.__init__`. Add a `freeze(reason: str) -> None` method that sets `self.frozen = True` and calls `self.raise_event(WalletFrozen(...))`. Guard `deposit` and `withdraw` with `if self.frozen: raise BusinessRuleViolation("wallet-frozen", ...)` at the top of each method. Then write an event listener using `@event_listener` from `pyfly.eda` that logs a structured warning every time a `WalletFrozen` event is published.

3. **Express a business rule as a Specification.** Write a `MinimumBalance(Specification[Wallet])` that checks whether a wallet's balance is at or above a threshold amount (in minor units) passed to its `__init__`. Combine it with `IsInCurrency` from Listing 6.9 using `&` to produce a `premium_eligible(currency: Currency, threshold: int)` factory function. Call `list(filter(premium_eligible(Currency.EUR, 50000), wallets))` over a list of test wallets and assert that only wallets with at least 500.00 EUR (50 000 cents) appear in the result.
