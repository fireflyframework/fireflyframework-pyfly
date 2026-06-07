<span class="eyebrow">Chapter 6</span>

# Domain-Driven Design {.chtitle}

::: figure art/openers/ch06.svg | &nbsp;

Lumen's wallet feature works. Deposits land, balances update, and the database persists everything across restarts. But look closely at the service layer and you will notice something uncomfortable: the overdraft check lives in the service method, not in the wallet itself. The currency validation is a property comparison scattered across a handful of `if`-statements. Nothing stops a future developer — or a future you at 11 pm — from bypassing those guards by calling `repo.save(entity)` directly.

**Domain-Driven Design** addresses this by making the model responsible for its own rules. Data stops being a passive bag of values that any caller can mutate; it becomes an object with opinions — one that enforces its invariants, announces what happened, and cooperates with the persistence layer while remaining free of any database import.

This chapter promotes the wallet into a proper DDD aggregate: a `Money` value object, a `Wallet` aggregate root that guards the overdraft rule and the currency-match rule, and a set of `DomainEvent`s emitted every time the wallet changes state. A thin mapper converts between the rich domain model and the flat persistence record — neither side needs to know the shape of the other.

---

## Entities and value objects

Before you can build a model that enforces its own rules, you need a vocabulary for the two fundamentally different kinds of objects that appear in every domain.

Think about what makes two wallets distinct. Even if two wallets happen to hold exactly one hundred euros, they are still separate wallets belonging to separate owners — you care *which one* you have. Now think about the amount itself. One hundred euros is one hundred euros; the exact Python object that holds the value is irrelevant. If a deposit adds fifty euros to a wallet's balance, you do not want to update the existing amount in place — you want to derive a brand-new amount that records the result. Mutating in place invites aliasing bugs where two parts of the code unknowingly share a reference to the same object and see each other's changes.

DDD names these two roles **entities** and **value objects**, and PyFly's `pyfly.domain` module makes them first-class concepts:

| Concept | PyFly base | Equality | Mutation |
|---|---|---|---|
| **`Entity[TID]`** | `Entity` | Identity — equal only when `id` matches | Allowed through owned methods |
| **`ValueObject`** | `ValueObject` | Structural — all fields compared | Forbidden; `replace(**changes)` creates a new instance |

Transient entities (those with `id=None`) compare equal only by Python's object identity, so you can safely put entities in sets and dicts without worrying about hash collisions from unsaved objects.

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

**How it works.** The amount is stored in **minor units** — integer cents, pence, or whatever the currency's smallest denomination is — to eliminate floating-point rounding entirely. Financial calculations that use `float` are a chronic source of off-by-one-cent bugs that only surface in production, usually during reconciliation. Storing €10.50 as `amount=1050` keeps all arithmetic exact. `__post_init__` rejects non-integer amounts immediately with `BusinessRuleViolation("money-amount-integer")`, so a stray float like `10.5` never silently enters the model.

The `currency` field uses the `Currency` enum (`Currency.EUR`, `Currency.USD`, `Currency.GBP`) rather than a bare string, ruling out typos at construction time. Currency comparisons inside `_assert_same_currency` use Python's identity check (`is`) — raising `BusinessRuleViolation("money-currency-mismatch")` if they differ — so the error surfaces exactly where the mistake was made.

Both `add` and `subtract` return a *new* `Money` instance rather than modifying `self`, a direct consequence of `frozen=True`. This immutability guarantee means the aggregate holding a `Money` value can never be partially updated: either the whole replacement succeeds or the old value remains in place. The `is_positive` and `is_negative` properties expose the sign without leaking the raw integer. `major_units` converts to a decimal for display, and `__str__` formats as `"10.50 EUR"` via `currency.value`.

!!! note "Minor units vs decimal"
    Storing money as integer cents is one convention; another is Python's `decimal.Decimal` with a fixed scale. Both are valid. What matters is picking one and sticking to it within the bounded context. For Lumen, integer minor units keep the model free of import-time precision configuration, and `__post_init__` enforces the constraint with `BusinessRuleViolation("money-amount-integer")` so a float never silently enters the model.

!!! spring "Spring parity"
    `ValueObject` mirrors the `@ValueObject` / `@Embeddable` cluster in Spring's JPA ecosystem and the `ValueObject` marker interface from Spring Modulith. The `frozen=True` dataclass maps to Java's `record` type introduced in Java 16 — immutable, value-based equality, concise syntax. jMolecules's `@ValueObject` annotation carries the same intent.

---

## The aggregate root

`Money` solves the representation problem — amounts are now immutable and currency-aware. But Lumen still needs something to *own* the wallet's balance and decide when a deposit or withdrawal is permitted. That is the role of the **aggregate root**.

An entity becomes an aggregate root when it owns a cluster of related objects and acts as the single point of entry for all changes within that cluster. The aggregate root is the **consistency boundary**: no external code reaches inside and mutates an inner object directly. All changes flow through the root's methods, which enforce the rules. This is the design that prevents the 11 pm bypass described in the chapter introduction — once every change must flow through the root, there is no back-channel.

`AggregateRoot[TID]` extends `Entity[TID]` with one addition: an internal buffer of **pending domain events**. Every state-changing method calls `self.raise_event(event)` to record what happened. When the repository saves the aggregate, the application service drains that buffer with `clear_events()` and publishes the events to the event bus. You will see the full publish cycle in the Domain events section; for now, focus on the aggregate itself.

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

**How it works.** The aggregate boundary is enforced at three levels. First, `__slots__` locks the attribute set, and `balance` and `owner_id` are deliberately public-but-owned: only the aggregate's own methods (`deposit`, `withdraw`) mutate them, while the `currency` property is a read-only convenience that delegates to `balance.currency`. Second, the factory classmethod `open` is the sole legitimate way to create a new wallet: the caller supplies the `wallet_id` (so the application layer controls ID generation), `open` validates that `owner_id` is non-blank, initializes the balance with `Money.zero(currency)`, and immediately queues `WalletOpened`. Using a factory rather than calling `__init__` directly ensures the opening event is *never* forgotten, even in a test fixture. Third, the domain events — `WalletOpened`, `FundsDeposited`, `FundsWithdrawn` — are frozen dataclasses. `FundsDeposited` and `FundsWithdrawn` carry a `balance` field (the post-operation balance in minor units), so a subscriber never needs to call back into the aggregate to learn the current state.

The diagram below shows the complete picture: state, invariants, and the events the wallet emits.

::: figure art/figures/06-aggregate.svg | Figure 6.1 — The Wallet aggregate: state, invariants, and the events it emits.

!!! spring "Spring parity"
    `AggregateRoot[str]` maps to jMolecules's `org.jmolecules.ddd.types.AggregateRoot<A, ID>` and to Spring Data's `AbstractAggregateRoot<A>`, which offers the same `registerEvent()` / `@DomainEvents` / `@AfterDomainEventPublication` mechanism. The pattern is identical in spirit: the aggregate accumulates events in a buffer; the repository drains them after a successful save; a `DomainEventPublisher` dispatches them. PyFly's `raise_event` + `clear_events` is the Python equivalent of `registerEvent` + `@AfterDomainEventPublication`.

---

## Protecting invariants

The aggregate root is only valuable if the rules it enforces are genuinely unreachable by any other path. That is what **invariant** means in DDD: a condition the model must uphold regardless of how it is called, who calls it, or how many services exist in the application. An invariant is not a suggestion — it is a constraint that cannot be violated because the model exposes no mechanism to do so.

Lumen's `Wallet` has three invariants:

1. The balance must never go below zero (no overdraft).
2. Funds can only be deposited or withdrawn in the wallet's native currency.
3. Deposit and withdrawal amounts must be strictly positive.

All three are enforced inside the aggregate methods. The framework exception for this is **`BusinessRuleViolation`** from `pyfly.domain`. It takes two required arguments: a stable machine-readable `rule` slug and a human-readable `message`. Lumen's slugs — `"wallet-insufficient-funds"`, `"wallet-currency-mismatch"`, `"wallet-deposit-positive"`, `"wallet-withdrawal-positive"` — are kebab-cased labels that travel in the RFC 7807 response body and in structured log fields.

`BusinessRuleViolation` extends `pyfly.kernel.BusinessException`, so the RFC 7807 problem-details mapper from Chapter 4 translates it automatically into an HTTP 422 response — no extra handler required.

!!! warning "Keep invariants in the model, not the service"
    Moving the overdraft check back into `WalletService` creates two problems. First, any code that calls `repo.save(entity)` directly bypasses the check entirely. Second, you end up duplicating the rule across every path that modifies a wallet — the service, a background job, an admin command. When the rule changes — say, the product team introduces a configurable overdraft buffer — there is exactly one place to update: the aggregate method. That is the whole point.

The difference between a service-level guard and an aggregate invariant is enforceability. A service guard is a convention; an aggregate invariant is a physical constraint enforced by encapsulation. To make that concrete, here is what the service-level approach looks like and why it is fragile:

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

**How it works.** The after version reads as an instruction: `wallet.withdraw(...)` means "ask the wallet to withdraw". The service does not know — or care — what that entails. It trusts the aggregate to either succeed or raise a `BusinessRuleViolation`. That thin-orchestrator pattern has a practical benefit for team workflows: a new developer can implement a `transfer` endpoint without reading `WalletService` at all. The constraints live in `Wallet` — one place to look.

The `rule` slug on `BusinessRuleViolation` matters too. Strings like `"wallet-insufficient-funds"` and `"wallet-currency-mismatch"` travel in the RFC 7807 response body, where client code can match them without parsing free-text messages. They also appear in structured log fields, making production alerts straightforward to write.

!!! note "AggregateNotFound"
    `AggregateNotFound` is the second domain exception in `pyfly.domain`. Raise it when the requested aggregate does not exist — it maps to a 404 problem-details response via the same RFC 7807 handler. The constructor takes the aggregate type name and the ID: `AggregateNotFound("Wallet", wallet_id)`.

---

## Domain events

Your aggregate now enforces its invariants and controls all state changes. But Lumen will eventually need to react to those changes: update an audit log, send a push notification, trigger fraud detection, publish a ledger entry. The tempting solution is to put those side-effects directly inside `deposit` and `withdraw`. That couples the domain model to infrastructure — suddenly your wallet needs to know about Kafka topics and email templates, and every unit test drags in a broker connection.

**Domain events** cut that coupling. A domain event records something that *happened* inside the aggregate — past tense, immutable fact. The aggregate does not know what will be done with the fact; it only records it. Downstream consumers — event listeners, projectors, notification services — subscribe and react in their own context, without the aggregate ever depending on them.

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

**How it works.** Each event class declares only the fields unique to that occurrence. Everything else comes from `DomainEvent`: a UUID `event_id` for idempotent processing, `occurred_at` for the audit trail, and `event_type` — the class name — for consumer routing without inspecting the Python class. All fields default to zero or empty string so that the `frozen=True` dataclass machinery can provide keyword-argument construction without requiring positional arguments.

Notice that `FundsDeposited` carries both `amount` (the transaction) and `balance` (the post-operation balance, in minor units). A subscriber updating a read-model balance needs no callback into the aggregate or the database — everything is in the event. That self-contained design keeps consumers simple and eliminates extra round-trips.

The event lifecycle spans two phases. Inside the aggregate: when `wallet.deposit(amount)` succeeds, it calls `self.raise_event(FundsDeposited(...))`, appending the event to a private buffer in `AggregateRoot`. Nothing is published yet. At the service boundary: after the repository saves the aggregate and the transaction commits, the application service drains the buffer and publishes. This *save-first, publish-after* sequence guarantees that an event is never dispatched for a change that failed to persist. Listing 6.6 shows that boundary in full:

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
        repo: object,              # typed as WalletRepository in practice
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

**How it works.** `open_wallet` calls `Wallet.open`, which queues a `WalletOpened` event internally; after the `save`, `wallet.clear_events()` returns that one event and `publish` dispatches it. `deposit` follows the same three-step pattern: load, mutate, save — then drain. The `for event in wallet.clear_events()` loop is intentionally explicit rather than hidden inside the repository, because the application service is the right place to decide *when* publishing happens — after the transaction boundary, not before.

!!! tip "Event ordering"
    `raise_event` appends to the buffer in call order. `clear_events` drains and clears it, returning events in the same order. If a single aggregate method raises multiple events (a batch operation, for example), they arrive at the event bus in the order they were raised — oldest first.

---

## Domain vs persistence

With the domain model and its events in place, one tension remains: how does the `Wallet` aggregate reach the database?

The tempting shortcut is to annotate `Wallet` directly with SQLAlchemy `Mapped[]` fields and a `__tablename__`. That merges two concerns that change at very different rates: business rules evolve with the product; column definitions evolve with the schema. Mixing them means a schema change forces you to touch the aggregate, and a rule change risks accidentally breaking a column mapping. It also drags SQLAlchemy into every unit test.

The alternative is two models that coexist without knowing about each other — and a thin mapper that converts between them:

| Model | Contains | Knows about |
|---|---|---|
| `Wallet` | Business rules, domain events, invariants | Nothing outside `pyfly.domain` |
| `WalletEntity` | Five columns: `id`, `owner_id`, `currency`, `balance_minor`, `created_at` | Only SQLAlchemy + `pyfly.data` |

`Wallet` is pure Python: no `Mapped[]` annotations, no `__tablename__`. You can instantiate it in a unit test with two lines and exercise every invariant without a database connection. `WalletEntity` is pure persistence: it subclasses `Base` from `pyfly.data.relational.sqlalchemy`, carries SQLAlchemy 2.0 typed columns, and knows nothing about domain rules or events. The framework's `Repository[WalletEntity, str]` (Chapter 5) stores and retrieves rows; a thin mapper converts between the row and the aggregate on every crossing.

Listing 6.7 shows `WalletEntity` — the ORM row — followed by the two mapper functions that cross the boundary:

::: listing lumen/models/entities/v1/wallet_orm.py | Listing 6.7 — WalletEntity: the SQLAlchemy persistence row
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

::: listing lumen/core/mappers/wallet_mapper.py | Listing 6.8 — wallet_mapper: pure functions that cross the domain/persistence boundary
from __future__ import annotations

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity


def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a Wallet aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )


def to_aggregate(entity: WalletEntity) -> Wallet:
    """Rehydrate a Wallet aggregate from a persistence row."""
    currency = Currency(entity.currency)
    return Wallet(
        id=entity.id,
        owner_id=entity.owner_id,
        balance=Money(amount=entity.balance_minor, currency=currency),
        created_at=entity.created_at,
    )
:::

**How it works.** `WalletEntity` subclasses `Base` rather than carrying any domain logic, so importing it registers the `wallets` table in `Base.metadata`; the framework's `EngineLifecycle` creates the table on startup when `ddl-auto=create` is set. The primary key is the aggregate's own string id (`wlt-…`) — not a surrogate — so the row and the `Wallet` share one identity and no translation is needed.

`to_entity` writes `wallet.balance.amount` (an integer) directly into `balance_minor` — no float conversion. `to_aggregate` reconstructs a `Currency` enum from the stored ISO-4217 string via `Currency(entity.currency)`, then builds a `Money` from the raw integer minor-unit value. The `created_at` field is preserved on the round-trip so rehydrated aggregates carry their original timestamp. There is no floating-point boundary crossing anywhere.

The mapper is intentionally narrow. It does not enforce rules — `Wallet.__init__` and the behaviour methods do that. It does not publish events — the application service does that. It only translates shape.

**The repository.** The application service never interacts with `WalletEntity` directly. Instead a command handler calls `wallet_mapper.to_entity(wallet)` before persisting and `wallet_mapper.to_aggregate(entity)` after loading, while the framework `WalletRepository(Repository[WalletEntity, str])` handles all SQL. Chapter 5 covers `Repository` in full; the key point here is that `Wallet` itself never imports SQLAlchemy — the aggregate stays free of persistence concerns across both sides of the mapper boundary.

!!! spring "Spring parity"
    This two-model-plus-mapper structure is the Python equivalent of the pattern
    advocated in Vaughn Vernon's *Implementing Domain-Driven Design* for Spring:
    a `WalletJpaEntity` annotated with `@Entity` (the persistence row), a
    `Wallet` domain object (the aggregate), and a `WalletAssembler` or
    MapStruct-generated mapper that translates between them. Spring Data JPA's
    `JpaRepository<WalletJpaEntity, String>` corresponds to PyFly's
    `Repository[WalletEntity, str]`. The structure is identical; the boilerplate
    is less.

---

## Specifications for business rules

The aggregate guards state-changing operations well — you cannot overdraw a wallet or deposit the wrong currency. But not every rule is about mutation. Some rules are eligibility checks: "before we show this user the withdrawal button, is the wallet in an operable state?" or "out of ten thousand wallets, which ones qualify for the loyalty bonus?" These are read-only predicates, and encoding them as aggregate methods would clutter `Wallet` with query logic unrelated to state transitions.

The **Specification pattern** solves this cleanly. A specification is a named, reusable predicate: a single `is_satisfied_by(obj) -> bool` method wrapped in an object that composes with others using Boolean operators. Because each rule is its own class, you can name rules clearly, reuse them across services, and combine them at runtime based on context — something an `if` chain cannot do.

`Specification[T]` from `pyfly.domain` is a composable in-memory predicate. Subclass it, implement `is_satisfied_by`, and combine instances with `&` (and), `|` (or), and `~` (not). A specification is also directly callable, so you can pass it to Python's built-in `filter` without any adapter code.

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

**How it works.** `HasPositiveBalance` delegates to `wallet.balance.is_positive` (a property, no parentheses). `IsInCurrency` uses identity comparison (`is`) because `Currency` is a `StrEnum` with singleton members. The `eligible_for_withdrawal` factory combines them with `&`, producing a composite whose `is_satisfied_by` returns `True` only when both checks pass. Because `Specification` implements `__call__`, you pass the composite directly to `filter()` — no lambda wrapper needed.

The key design discipline: a specification is a *predicate*, not a *guard*. It returns `True` or `False` and never raises. Aggregate invariants (overdraft, currency mismatch) belong inside `deposit` and `withdraw` because they must *prevent* a state change. Specifications belong in services and query handlers because they *select* or *classify* — they never mutate.

Specifications are especially useful where rules combine dynamically: an admin search that adds filters based on the operator's role, or a batch job that partitions a list into eligible and ineligible wallets. Each class has exactly one method and no side-effects, making isolated unit tests trivial.

!!! tip "Specification.of for quick lambdas"
    For one-off predicates that do not need a class, use the factory method: `spec = Specification.of(lambda w: w.balance.amount >= 1000, name="minimum-balance")`. It composes with `&`, `|`, and `~` the same way as a full subclass.

---

## What you built {.recap}

Lumen's wallet is now a first-class domain model.

`Money` is a frozen `ValueObject` that stores amounts as integer minor units, enforces currency homogeneity via `_assert_same_currency`, and is replaced rather than mutated. `__post_init__` rejects floats immediately. `is_positive` and `is_negative` are properties; `major_units` and `__str__` handle display.

`Wallet(AggregateRoot[str])` is the consistency boundary. Its factory `open` and behaviour methods `deposit`/`withdraw` enforce all three invariants — no overdraft, no cross-currency operations, no non-positive amounts — by raising `BusinessRuleViolation` with a stable rule slug. Every state change queues a domain event (`WalletOpened`, `FundsDeposited`, `FundsWithdrawn`); the post-operation balance is recorded in each event so subscribers need no callback. After a successful save the application service drains events with `clear_events()` and hands them to `EventPublisher`.

The persistence layer sees only `WalletEntity` (five columns, no domain logic). `to_aggregate` in `wallet_mapper` rehydrates the row into a `Wallet`, and `to_entity` flattens it back; the framework `WalletRepository(Repository[WalletEntity, str])` handles all SQL without the aggregate ever importing SQLAlchemy. `Specification[Wallet]` gives you a composable, callable predicate for eligibility checks that live outside the aggregate boundary.

The controller is untouched. The service shrank. The rules are enforced by the object that owns them.

---

## Try it yourself {.exercises}

1. **Add a `transfer_to` method and a `DailyLimit` value object.** Add a `DailyLimit(ValueObject)` with `max_amount: int` and `currency: Currency`, decorated with `@dataclass(frozen=True)`. Then add `Wallet.transfer_to(target: Wallet, amount: Money) -> None`. The method should call `self.withdraw(amount)` and `target.deposit(amount)` in sequence, raising `BusinessRuleViolation("transfer-currency-mismatch", ...)` if the two wallets hold different currencies. Because `Wallet` uses `__slots__`, add `"_frozen"` to the tuple if you also do exercise 2. Verify that both wallets each accumulate a `FundsWithdrawn` / `FundsDeposited` event, respectively, and that a transfer between mismatched wallets raises before modifying either balance.

2. **Add a `WalletFrozen` event and a `freeze()` behaviour.** Define `WalletFrozen(DomainEvent)` with `wallet_id: str = ""` and `reason: str = ""`. Add `"frozen"` to `__slots__` and a `frozen: bool` attribute (default `False`) to `Wallet.__init__`. Add a `freeze(reason: str) -> None` method that sets `self.frozen = True` and calls `self.raise_event(WalletFrozen(...))`. Guard `deposit` and `withdraw` with `if self.frozen: raise BusinessRuleViolation("wallet-frozen", ...)` at the top of each method. Then write an event listener using `@event_listener` from `pyfly.eda` that logs a structured warning every time a `WalletFrozen` event is published.

3. **Express a business rule as a Specification.** Write a `MinimumBalance(Specification[Wallet])` that checks whether a wallet's balance is at or above a threshold amount (in minor units) passed to its `__init__`. Combine it with `IsInCurrency` from Listing 6.9 using `&` to produce a `premium_eligible(currency: Currency, threshold: int)` factory function. Call `list(filter(premium_eligible(Currency.EUR, 50000), wallets))` over a list of test wallets and assert that only wallets with at least 500.00 EUR (50 000 cents) appear in the result.
