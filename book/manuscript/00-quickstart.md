<span class="eyebrow">Quick Start</span>

# Build Lumen Step by Step {.chtitle}

::: figure art/openers/ch01.svg | &nbsp;

Welcome. This is the very first thing you will build with PyFly, and we are going to take it slowly. By the end of this chapter you will have gone from an *empty folder* to a *running, tested* slice of a real digital-wallet service — opening a wallet, persisting it to a database, reading its balance back over HTTP, and reacting to a domain event. Every concept gets a small, complete piece of code and a "Run it" checkpoint so you can see it working before moving on.

This is a *tour*, not the deep dive. Each step previews a topic that Part I and Part II cover thoroughly later. The goal here is momentum: by the time you reach Chapter 1 you will have already met dependency injection, configuration, HTTP, persistence, CQRS, and events — in the small — and the rest of the book will fill in the *why*.

The application you build is called **Lumen**: a DDD-flavoured digital wallet. A wallet can be opened, deposited to, and withdrawn from, protecting one core rule — **the balance never goes negative** — and modelling money with exact integer arithmetic so there is never any floating-point drift. It is the same application the entire book builds, so nothing you learn here is throwaway.

!!! note "Note"
    This chapter is written against PyFly **v26.6.110**. Every listing is taken from the real, running `samples/lumen` project that ships with the book — the code compiles, boots, and passes its tests. You will recognise these exact files again, in more depth, in later chapters.

---

## Step 1 — Prerequisites and install

PyFly is a Python framework, and the smoothest way to work with it is through [**uv**](https://docs.astral.sh/uv/), Astral's fast Python package and project manager. uv handles your Python version, your virtual environment, and your dependencies in one tool, and the `pyfly` command-line tool runs through it.

You need two things:

* **Python 3.12 or newer.** PyFly uses modern typing features (`StrEnum`, `X | None` unions, PEP 695 generics).
* **uv.** Install it once, system-wide.

Install uv with the official one-liner:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # macOS / Linux
# Windows (PowerShell):
#   powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Run it

Confirm both tools are available:

```bash
uv --version
uv python install 3.12   # ensures a 3.12+ interpreter is available
```

You should see a uv version printed, and uv will report that Python 3.12 is installed (or already present).

!!! tip "Following along with the finished sample"
    Everything in this chapter exists, finished, in the book's repository under `samples/lumen`. If at any point you want to compare your code with the real thing — or just run it — clone the repo and do:

    ```bash
    cd samples/lumen
    uv sync --extra dev        # framework + pytest
    uv run pyfly run --server uvicorn
    ```

    You can build alongside it, copying one file at a time, or read the finished version when a step is unclear. Both work.

---

## Step 2 — Scaffold the project

PyFly ships a project generator, `pyfly new`, the same way Spring has the Spring Initializr. It writes a conventional project layout so you do not start from a blank page. Because `pyfly` lives inside the framework package, the very first thing we do is create a project directory and add PyFly to it.

For this tour we will build the directories by hand as we go — it keeps every file visible and nothing hidden behind a generator — but the generator is there when you want a head start:

```bash
pyfly new lumen --archetype hexagonal --features web,data-relational
```

That command creates a `lumen/` folder, a `pyproject.toml`, a `pyfly.yaml`, and a layered source tree. Let's create the same shape ourselves so you see exactly what each piece is for. Start with the project and its dependencies:

```bash
mkdir lumen && cd lumen
uv init --package --name lumen
uv add "pyfly[cli,web,data-relational]" "pydantic>=2.5"
uv add --dev "pytest>=8" "pytest-asyncio>=0.24" "httpx>=0.27"
```

The three PyFly extras you just added map to the three things Lumen needs: `cli` brings the `pyfly` command itself, `web` brings the ASGI server, and `data-relational` brings SQLAlchemy 2 (async) plus `aiosqlite` so we can persist to a SQLite file with no external database to install.

### The project layout

PyFly applications follow a layered structure that separates the public contract, the domain model, the application logic, and the web edge. Create these packages under `src/lumen`:

```
lumen/
├── pyproject.toml
├── pyfly.yaml                 # framework configuration
└── src/lumen/
    ├── interfaces/            # the public contract: DTOs + enums
    │   ├── dtos/v1/
    │   └── enums/v1/
    ├── models/                # the domain model + persistence
    │   ├── entities/v1/
    │   └── repositories/
    ├── core/                  # application logic: commands, queries, handlers
    │   ├── services/
    │   └── mappers/
    ├── web/                   # the HTTP edge: controllers
    │   └── controllers/
    ├── app.py                 # the application class
    └── main.py                # the ASGI entry point
```

Each layer has one job. `interfaces` is the boundary other code (and other services) talks to. `models` holds the rich domain objects and the rows they persist as. `core` holds the business operations. `web` exposes them over HTTP. We will fill these in one layer at a time.

!!! spring "Spring parity"
    `pyfly new` is the equivalent of the Spring Initializr (`start.spring.io`). The `web` and `data-relational` features are the PyFly counterparts of the `spring-boot-starter-web` and `spring-boot-starter-data-jpa` starters — naming a feature pulls in exactly the dependencies and auto-configuration that feature needs, and nothing more.

### The application class

Two files turn a package into a PyFly application. The first, `app.py`, declares the application itself: which packages to scan for components and which framework tiers to switch on.

::: listing lumen/app.py | Listing 0.1 — The application class
from __future__ import annotations

from pyfly.core import pyfly_application
from pyfly.starters.domain import enable_domain_stack


@enable_domain_stack
@pyfly_application(
    name="lumen",
    version="1.0.0",
    description="Lumen — a DDD digital-wallet service built on the PyFly framework.",
    scan_packages=[
        "lumen.models.repositories",
        "lumen.core.services.wallets",
        "lumen.web.controllers",
    ],
)
class LumenApplication:
    pass
:::

`@pyfly_application` marks the class as a PyFly app and `scan_packages` tells the dependency-injection container where to look for the components you will declare — your repositories, services, command/query handlers, and controllers. `@enable_domain_stack` switches on the domain tiers we will lean on later: CQRS, the transactional engine, the relational data layer, and events.

### The ASGI entry point

The second file, `main.py`, is what an ASGI server actually imports and serves. It bootstraps PyFly — loads configuration, scans your packages, and builds the application context — then hands the resulting web application to Starlette.

::: listing lumen/main.py | Listing 0.2 — The ASGI entry point
from __future__ import annotations

from lumen.app import LumenApplication
from pyfly.core import PyFlyApplication
from pyfly.web.adapters.starlette import create_app

# Bootstrap: load config, scan packages, build the DI context.
_pyfly = PyFlyApplication(LumenApplication)

app = create_app(
    title="lumen",
    version="1.0.0",
    description="Lumen — a DDD digital-wallet service built on the PyFly framework.",
    context=_pyfly.context,
)
:::

!!! note "Note"
    The real `samples/lumen/main.py` adds a `lifespan` hook and a `/static` mount. Those are refinements you will meet in Chapter 4; the two essentials — `PyFlyApplication(LumenApplication)` to bootstrap, and `create_app(...)` to build the web app — are exactly what you see here.

### Configuration

PyFly reads `pyfly.yaml` from the project root. Create one that names the app, sets the HTTP port, and switches on the tiers we need. Everything is nested under a top-level `pyfly` key.

::: listing pyfly.yaml | Listing 0.3 — pyfly.yaml
pyfly:
  app:
    name: lumen
    version: 1.0.0
  server:
    # App on 8080; the actuator + admin default to the management port 9090.
    port: 8080
  cqrs:
    enabled: true
  transactional:
    enabled: true
  eda:
    provider: memory          # in-memory event bus, no broker needed
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create        # create tables on startup
:::

A few keys are worth a moment now and a chapter later. `pyfly.server.port` is the application's HTTP port — `8080` by default, exactly like Spring's `server.port`. `data.relational` points at a SQLite file (`lumen.db`) and `ddl-auto: create` tells the framework to create the database schema on startup, so there is no migration step to run for this tour. `eda.provider: memory` gives us an in-process event bus.

!!! warning "Warning"
    If you are coming from an older PyFly, note that the port key is `pyfly.server.port` (env override `PYFLY_SERVER_PORT`). The old `pyfly.web.port` / `PYFLY_WEB_PORT` were removed — set the port under `pyfly.server` from now on.

### Run it

Even with no endpoints yet, the application boots. Start it:

```bash
uv run pyfly run --server uvicorn
```

You will see the PyFly banner, structured startup logs, and a line telling you the server is listening on `http://0.0.0.0:8080`. The `--server uvicorn` flag selects the Uvicorn server (it comes with `pyfly[web]`); for development, add `--reload` to restart automatically when you edit a file.

PyFly also exposes a **health endpoint** so orchestrators can tell the app is alive. Actuator endpoints and the admin dashboard run on a *separate management port*, `9090` by default, which keeps operational endpoints off your public application port. In another terminal:

```bash
curl -s localhost:9090/actuator/health
```

```json
{"status":"UP"}
```

!!! note "Two ports, on purpose"
    The application serves your API on `8080`; the **management port** `9090` serves `/actuator/health`, `/actuator/info`, and the admin dashboard. This is Spring Boot's `management.server.port` behaviour. The management port is *open and unauthenticated by default* — fine on a private network, but in production you would set `pyfly.management.security.enabled: true` to lock it down, or `pyfly.management.server.port: -1` to disable management endpoints entirely. By default only `health` and `info` are exposed over HTTP; expose more (metrics, env, …) with `pyfly.management.endpoints.web.exposure.include`.

Stop the server with `Ctrl-C`. The shell is empty, but the foundation is live: the DI container builds, the server starts, and health reporting works. Now we give it something to do.

---

## Step 3 — The first slice of the domain

We start where DDD says to start: with the model. Two objects carry the whole domain — `Money`, a value object for exact amounts, and `Wallet`, the aggregate that owns the balance.

### Money — a value object

`Money` is the textbook *value object*: it has no identity, two instances with the same amount and currency are interchangeable, and it never changes. We store amounts as integer **minor units** (cents) plus an ISO-4217 currency code, so arithmetic is exact — `Money(1050, EUR)` is €10.50, and there is no floating-point rounding to worry about.

First the tiny currency enum it depends on, under `interfaces/enums/v1/`:

::: listing lumen/interfaces/enums/v1/currency.py | Listing 0.4 — The Currency enum
from __future__ import annotations

from enum import StrEnum


class Currency(StrEnum):
    """ISO-4217 currency codes Lumen wallets can hold."""

    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"
:::

Now `Money` itself, under `models/entities/v1/`. It builds on `pyfly.domain.ValueObject` and is a frozen dataclass, so equality is structural and instances are immutable. Arithmetic returns *new* `Money` objects and refuses to mix currencies.

::: listing lumen/models/entities/v1/money.py | Listing 0.5 — The Money value object
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.domain import BusinessRuleViolation, ValueObject


@dataclass(frozen=True)
class Money(ValueObject):
    """An exact monetary amount in a single currency (minor units)."""

    amount: int
    currency: Currency

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise BusinessRuleViolation(
                "money-amount-integer", "amount must be an integer number of minor units"
            )

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        """The additive identity for *currency* (a zero balance)."""
        return cls(amount=0, currency=currency)

    def add(self, other: Money) -> Money:
        self._assert_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def subtract(self, other: Money) -> Money:
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
        """The amount as a major-unit decimal (cents / 100)."""
        return round(self.amount / 100, 2)

    def _assert_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise BusinessRuleViolation(
                "money-currency-mismatch",
                f"cannot combine {self.currency.value} with {other.currency.value}",
            )
:::

`BusinessRuleViolation` is the framework's signal that a domain rule was broken — here, "amounts are whole minor units" and "you cannot add euros to dollars". Notice there is no HTTP, no database, no framework wiring: a value object is pure domain.

### Wallet — the aggregate

The `Wallet` is the *aggregate root*: the object that owns the invariant. State only changes through intent-revealing methods (`open`, `deposit`, `withdraw`), each of which protects the rule **balance never goes negative** and records a *domain event* describing what happened. Built on `pyfly.domain.AggregateRoot`, it raises events with `raise_event(...)`, which we will drain and publish in Step 9.

::: listing lumen/models/entities/v1/wallet_entity.py | Listing 0.6 — The Wallet aggregate and its domain events
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

    @classmethod
    def open(cls, wallet_id: str, owner_id: str, currency: Currency) -> Wallet:
        """Open a new, empty wallet; raises :class:`WalletOpened`."""
        if not owner_id.strip():
            raise BusinessRuleViolation("wallet-owner-required", "owner_id is required")
        wallet = cls(id=wallet_id, owner_id=owner_id, balance=Money.zero(currency))
        wallet.raise_event(
            WalletOpened(wallet_id=wallet_id, owner_id=owner_id, currency=currency.value)
        )
        return wallet

    def deposit(self, amount: Money) -> None:
        """Credit *amount* to the balance; raises :class:`FundsDeposited`."""
        self._assert_currency(amount)
        if not amount.is_positive:
            raise BusinessRuleViolation("wallet-deposit-positive", "deposit amount must be > 0")
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

    def _assert_currency(self, amount: Money) -> None:
        if amount.currency is not self.balance.currency:
            raise BusinessRuleViolation(
                "wallet-currency-mismatch",
                f"wallet holds {self.balance.currency.value}, got {amount.currency.value}",
            )
:::

::: figure art/figures/06-aggregate.svg | Figure 0.1 — The Wallet aggregate owns its invariant; all state changes go through its methods.

!!! note "Note"
    The finished `Wallet` in `samples/lumen` also has a `withdraw` method and a `FundsWithdrawn` event, which follow the same shape — we have left them out here to keep this first listing short. Chapter 6 builds the full aggregate.

### Run it

You do not need the server running to exercise the domain. Open a Python REPL inside the project and drive the model directly:

```bash
uv run python
```

```python
>>> from lumen.interfaces.enums.v1.currency import Currency
>>> from lumen.models.entities.v1.money import Money
>>> from lumen.models.entities.v1.wallet_entity import Wallet
>>> w = Wallet.open("wlt-1", "alice", Currency.EUR)
>>> w.deposit(Money(1500, Currency.EUR))
>>> w.balance.amount, w.balance.currency.value
(1500, 'EUR')
>>> w.deposit(Money(100, Currency.USD))     # wrong currency → rejected
pyfly.domain.exceptions.BusinessRuleViolation: wallet holds EUR, got USD
```

The aggregate enforces its own rules in pure Python — no infrastructure required.

---

## Step 4 — Persist it

A wallet that lives only in memory is not much use. We need to save it. PyFly's data layer gives you a *Spring-Data-style repository* over SQLAlchemy, and because we configured SQLite there is no database to install.

### The persistence row

The aggregate is rich; the row it persists as is flat. We map `Wallet` onto a `WalletEntity` — one row per wallet, with the balance split into an integer column (`balance_minor`) and a currency column. It inherits the framework's declarative `Base`, which lets it keep the aggregate's own string id as its primary key.

::: listing lumen/models/entities/v1/wallet_orm.py | Listing 0.7 — The WalletEntity persistence row
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's own string id."""

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))
:::

Because the class subclasses `Base`, importing it registers the `wallets` table; with `ddl-auto: create` the framework creates that table on startup.

### The repository

Instead of hand-writing SQL, you subclass the framework's generic `Repository[Entity, IdType]`. That single declaration tells the framework the entity type and the primary-key type, and in return you get the full async repository surface for free — `save`, `find_by_id`, `find_all`, `count`, `delete`, paging, and more — with the database session injected for you.

::: listing lumen/models/repositories/wallet_repository.py | Listing 0.8 — A Spring-Data-style repository
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD for :class:`WalletEntity`, plus a convenience upsert."""

    async def find_by_owner_id(self, owner_id: str) -> list[WalletEntity]:
        """All wallets owned by *owner_id* (derived query stub)."""
        ...

    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Insert *entity*, or update the row with the same id."""
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

Two things here are pure Spring Data. `find_by_owner_id` is a **derived query** — its body is an elided stub (`...`), and at startup the framework parses the method *name* and compiles a real `SELECT … WHERE owner_id = :owner_id` for you. `upsert` is a small convenience over `session.merge` so a handler can persist a wallet whether it is new or already exists, with a single call.

The `@repository` decorator registers the class as a managed component — a *bean* in the DI container — so it can be injected into the handlers we write next.

::: figure art/figures/05-repository.svg | Figure 0.2 — A framework Repository turns a typed declaration into a full CRUD surface.

!!! spring "Spring parity"
    `Repository[WalletEntity, str]` is the direct analogue of Spring Data's `JpaRepository<WalletEntity, String>`. You declare the interface; the framework provides the implementation. Derived queries (`findByOwnerId` in Spring, `find_by_owner_id` here) are parsed from the method name in exactly the same way.

---

## Step 5 — A write path with CQRS

Now we wire the model to the repository through a *command*. PyFly uses **CQRS** — Command Query Responsibility Segregation — which means writes flow through one path (commands) and reads through another (queries). A command is a small, immutable object describing intent; a handler executes it.

### The command

`OpenWallet` carries the data needed to open a wallet and validates itself before anything runs. It is a frozen dataclass extending `Command[str]` — the `str` says "this command, when handled, produces a wallet id".

::: listing lumen/core/services/wallets/open_wallet_command.py | Listing 0.9 — The OpenWallet command
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.enums.v1.currency import Currency
from pyfly.cqrs import Command, ValidationResult


@dataclass(frozen=True)
class OpenWallet(Command[str]):
    """Open a new wallet. Returns the generated wallet id."""

    owner_id: str
    currency: Currency

    async def validate(self) -> ValidationResult:  # type: ignore[override]
        if not self.owner_id.strip():
            return ValidationResult.failure("owner_id", "Owner id is required")
        return ValidationResult.success()
:::

### The handler

The handler is where the work happens: generate an id, create the `Wallet` aggregate, persist it through the repository, then drain and publish the aggregate's events. It runs inside `@transactional()`, which opens a unit of work, commits on success, and rolls back on failure. The repository and the event publisher are injected by the container — you only declare them in `__init__`.

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listing 0.10 — The OpenWallet handler
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumen.core.mappers.wallet_mapper import to_entity
from lumen.core.services.wallets.event_publishing import publish_domain_events
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import CommandHandler, command_handler
from pyfly.data.relational.sqlalchemy import transactional
from pyfly.eda import EventPublisher


@command_handler
@service
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    """Open a new, empty wallet."""

    def __init__(
        self,
        repository: WalletRepository,
        events: EventPublisher,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
        self._session_factory = session_factory

    @transactional()
    async def do_handle(self, command: OpenWallet) -> str:  # type: ignore[override]
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))
        await publish_domain_events(self._events, wallet.clear_events())
        return wallet_id
:::

The handler calls `to_entity(wallet)` — a small mapper that flattens the aggregate into a `WalletEntity` row. Create it under `core/mappers/`. (We will add the read-side projection it also needs in the next step.)

::: listing lumen/core/mappers/wallet_mapper.py | Listing 0.11 — Mapping the aggregate to its row
from __future__ import annotations

from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.entities.v1.wallet_orm import WalletEntity


def to_entity(wallet: Wallet) -> WalletEntity:
    """Flatten a :class:`Wallet` aggregate into a persistable row."""
    assert wallet.id is not None
    return WalletEntity(
        id=wallet.id,
        owner_id=wallet.owner_id,
        currency=wallet.currency.value,
        balance_minor=wallet.balance.amount,
        created_at=wallet.created_at,
    )
:::

::: figure art/figures/07-cqrs.svg | Figure 0.3 — A command flows through the bus to its handler; queries take a separate path.

!!! spring "Spring parity"
    `@command_handler` + `@service` registers a handler the command bus dispatches to — much like a Spring `@Service` whose method handles a request. `@transactional()` is the PyFly counterpart of Spring's `@Transactional`: it manages the unit of work so the persist either fully commits or fully rolls back.

---

## Step 6 — A read path

Reads take the other lane. A *query* asks a question; a *query handler* answers it, typically projecting a database row onto a small, purpose-built DTO. We will read just the balance.

### The DTO and the query

The balance response is a tiny Pydantic model under `interfaces/dtos/v1/`:

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listing 0.12 — The BalanceDto response model
from __future__ import annotations

from pydantic import BaseModel

from lumen.interfaces.enums.v1.currency import Currency


class BalanceDto(BaseModel):
    """Lightweight balance projection for the balance endpoint."""

    id: str
    currency: Currency
    balance_minor: int
    balance: float
:::

The query carries just the wallet id and declares it returns a `BalanceDto` or `None`:

::: listing lumen/core/services/wallets/get_balance_query.py | Listing 0.13 — The GetBalance query
from __future__ import annotations

from dataclasses import dataclass

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from pyfly.cqrs import Query


@dataclass(frozen=True)
class GetBalance(Query[BalanceDto | None]):
    """Look up just the balance of a wallet by its identifier."""

    wallet_id: str
:::

### The handler and the projection

Add the read-side mapper to `wallet_mapper.py` — a small function that projects a row onto the DTO, computing the major-unit balance:

::: listing lumen/core/mappers/wallet_mapper.py | Listing 0.14 — Projecting a row onto the balance DTO
from __future__ import annotations

from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.wallet_orm import WalletEntity


def entity_to_balance_dto(entity: WalletEntity) -> BalanceDto:
    """Project a persisted row onto the lightweight balance DTO."""
    return BalanceDto(
        id=entity.id,
        currency=Currency(entity.currency),
        balance_minor=entity.balance_minor,
        balance=round(entity.balance_minor / 100, 2),
    )
:::

The query handler loads the row by id and projects it — returning `None` when there is no such wallet:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 0.15 — The GetBalance handler
from __future__ import annotations

from lumen.core.mappers.wallet_mapper import entity_to_balance_dto
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import service
from pyfly.cqrs import QueryHandler, query_handler


@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(self, query: GetBalance) -> BalanceDto | None:  # type: ignore[override]
        entity = await self._repository.find_by_id(query.wallet_id)
        return entity_to_balance_dto(entity) if entity is not None else None
:::

Notice the asymmetry CQRS gives you: the write side rehydrates the full aggregate to protect invariants; the read side touches only the columns the balance view needs and never constructs an aggregate at all. Each side is shaped for its own job.

---

## Step 7 — Expose it over HTTP

The domain works, it persists, and it has write and read paths. Now we put a web edge on it. A **controller** maps HTTP requests onto commands and queries and dispatches them through the bus — it holds no business logic of its own.

First the request DTO for opening a wallet, under `interfaces/dtos/v1/`:

::: listing lumen/interfaces/dtos/v1/open_wallet_request.py | Listing 0.16 — The OpenWalletRequest payload
from __future__ import annotations

from pydantic import BaseModel, Field

from lumen.interfaces.enums.v1.currency import Currency


class OpenWalletRequest(BaseModel):
    """Wallet-opening request payload."""

    owner_id: str = Field(min_length=1, max_length=64, description="Identifier of the wallet owner")
    currency: Currency = Field(default=Currency.EUR, description="ISO-4217 currency the wallet holds")
:::

Now the controller, under `web/controllers/`. The DI container injects the command and query buses; each handler builds a command or query and `await`s it on the bus. Parameter annotations declare where data comes from: `Valid[Body[...]]` binds and validates the JSON body, `PathVar[str]` binds a URL segment.

::: listing lumen/web/controllers/wallet_controller.py | Listing 0.17 — The wallet controller
from __future__ import annotations

from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.kernel import ResourceNotFoundException
from pyfly.web import Body, PathVar, Valid, get_mapping, post_mapping, request_mapping


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open a wallet, read its balance."""

    def __init__(self, commands: DefaultCommandBus, queries: DefaultQueryBus) -> None:
        self._commands = commands
        self._queries = queries

    @post_mapping("", status_code=201)
    async def open_wallet(self, request: Valid[Body[OpenWalletRequest]]) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(owner_id=request.owner_id, currency=request.currency)
        )
        return {"wallet_id": wallet_id}

    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(self, wallet_id: PathVar[str]) -> BalanceDto:
        result = await self._queries.query(GetBalance(wallet_id=wallet_id))
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result
:::

::: figure art/figures/04-request.svg | Figure 0.4 — A request binds to a handler, which dispatches a command or query through the bus.

### Run it

Start the server:

```bash
uv run pyfly run --server uvicorn
```

In another terminal, open a wallet:

```bash
curl -s -X POST localhost:8080/api/v1/wallets \
  -H 'content-type: application/json' \
  -d '{"owner_id":"alice","currency":"EUR"}'
```

```json
{"wallet_id":"wlt-7d2c1a9e-..."}
```

Read the balance back (substitute the id you got above):

```bash
curl -s localhost:8080/api/v1/wallets/wlt-7d2c1a9e-.../balance
```

```json
{"id":"wlt-7d2c1a9e-...","currency":"EUR","balance_minor":0,"balance":0.0}
```

A freshly opened wallet has a zero balance — exactly what `Money.zero(EUR)` produced back in the aggregate's `open` factory. The request travelled from HTTP, through the command bus, into the handler, through the repository, to SQLite, and back out the read path. That is the whole arc, end to end.

!!! tip "Interactive docs for free"
    While the server runs, open `http://localhost:8080/docs` in a browser. PyFly generated an OpenAPI document and a Swagger UI from your controller and DTOs — you can try the endpoints right there, no extra code.

---

## Step 8 — Prove it with a test

Running `curl` by hand is fine once; a test proves it forever. PyFly is designed to be testable without a running server — you can dispatch commands and queries straight through the buses. Write a test under `tests/`:

::: listing tests/test_quickstart.py | Listing 0.18 — An end-to-end test through the buses
from __future__ import annotations

import pytest
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.interfaces.enums.v1.currency import Currency

from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus


@pytest.mark.asyncio
async def test_open_wallet_then_read_balance(
    command_bus: DefaultCommandBus,
    query_bus: DefaultQueryBus,
) -> None:
    wallet_id = await command_bus.send(OpenWallet(owner_id="alice", currency=Currency.EUR))
    assert wallet_id.startswith("wlt-")

    balance = await query_bus.query(GetBalance(wallet_id=wallet_id))
    assert balance is not None
    assert balance.balance_minor == 0
    assert balance.currency is Currency.EUR
:::

The `command_bus` and `query_bus` parameters are *fixtures*: they boot the application context once and hand you wired buses, the same components the controller uses in production. (The finished `samples/lumen/tests/conftest.py` defines these fixtures; copy it when you build your own suite — Chapter 16 explains it in full.)

### Run it

```bash
uv run --extra dev pytest -q
```

```
.                                                       [100%]
1 passed in 0.42s
```

Green. You now have a wallet feature that is not just running but *verified* — the same test runs in CI on every change.

!!! spring "Spring parity"
    Dispatching through the buses in a test mirrors Spring Boot's slice tests: you exercise real wired beans without standing up the HTTP server. The `command_bus` / `query_bus` fixtures are the PyFly equivalent of an injected Spring `ApplicationContext` in an `@SpringBootTest`.

---

## Step 9 — A taste of events

The aggregate has been recording domain events all along — `WalletOpened`, `FundsDeposited` — and the handler drains them with `wallet.clear_events()` and publishes them. So far nothing has *listened*. Let's add a small listener that reacts.

First, the publishing bridge the handler already imports. It turns each drained domain event into a payload and publishes it on the event bus, under `core/services/wallets/`:

::: listing lumen/core/services/wallets/event_publishing.py | Listing 0.19 — Publishing drained domain events
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from lumen.core.services.listeners.wallet_audit_listener import WALLET_EVENTS_DESTINATION
from pyfly.domain import DomainEvent
from pyfly.eda import EventPublisher


def _to_payload(event: DomainEvent) -> dict[str, Any]:
    """Flatten a frozen-dataclass domain event into a JSON-friendly dict."""
    payload: dict[str, Any] = dataclasses.asdict(event)
    payload.setdefault("event_type", event.event_type)
    return payload


async def publish_domain_events(publisher: EventPublisher, events: Iterable[DomainEvent]) -> None:
    """Publish each drained domain event on the wallet events channel."""
    for event in events:
        await publisher.publish(
            destination=WALLET_EVENTS_DESTINATION,
            event_type=event.event_type,
            payload=_to_payload(event),
        )
:::

Now the listener, under `core/services/listeners/`. It is a plain `@service` whose method is stamped `@event_listener`; at startup PyFly discovers it and subscribes it to the bus — no wiring by hand. Here it keeps a tiny in-memory audit log and a running total per wallet.

::: listing lumen/core/services/listeners/wallet_audit_listener.py | Listing 0.20 — A domain-event listener
from __future__ import annotations

from pyfly.container import service
from pyfly.eda import EventEnvelope, event_listener

# The logical channel the wallet handlers publish domain events to.
WALLET_EVENTS_DESTINATION = "wallet.events"


@service
class WalletAuditListener:
    """In-memory audit log + running-total projection over wallet events."""

    def __init__(self) -> None:
        self._running_totals: dict[str, int] = {}

    @event_listener(event_types=["WalletOpened", "FundsDeposited"])
    async def on_wallet_event(self, envelope: EventEnvelope) -> None:
        payload = dict(envelope.payload)
        wallet_id = str(payload.get("wallet_id", ""))
        if envelope.event_type == "WalletOpened":
            self._running_totals.setdefault(wallet_id, 0)
        elif envelope.event_type == "FundsDeposited":
            amount = int(payload.get("amount", 0))
            self._running_totals[wallet_id] = self._running_totals.get(wallet_id, 0) + amount

    def running_total(self, wallet_id: str) -> int:
        """Net funds for *wallet_id*, in minor units."""
        return self._running_totals.get(wallet_id, 0)
:::

Add the listeners package to `scan_packages` in `app.py` so the container discovers it:

```python
scan_packages=[
    "lumen.models.repositories",
    "lumen.core.services.wallets",
    "lumen.core.services.listeners",   # <-- add this
    "lumen.web.controllers",
],
```

::: figure art/figures/08-eda.svg | Figure 0.5 — A handler publishes domain events; listeners subscribe and react, decoupled from the command.

### Run it

Open a wallet, then deposit, and the listener's running total updates as a side effect of those commands — without the command knowing the listener exists. That decoupling is the whole point of events: you add reactions (audit logs, notifications, projections) without touching the code that triggered them.

!!! spring "Spring parity"
    `@event_listener` is the PyFly counterpart of Spring's `@EventListener`. Publishing through an `EventPublisher` and subscribing with a stamped method is the same publish/subscribe model as Spring's `ApplicationEventPublisher` and `@EventListener`-annotated beans.

---

## What you built {.recap}

You just built — and tested — a real vertical slice of a service: a domain model, a database, a write path, a read path, an HTTP edge, and an event reaction. Every one of those was a *preview*. The rest of the book takes each one apart and rebuilds it properly, with the reasoning, the alternatives, and the production details.

Here is the map from what you just did to the chapter that goes deep:

| In this Quick Start you… | Goes deep in |
|---|---|
| Saw the container build and inject your beans (`@repository`, `@service`) | **Chapter 2** — Dependency Injection & the Application Context |
| Configured the app with `pyfly.yaml` and the management port | **Chapter 3** — Configuration, Profiles & Secrets |
| Exposed an HTTP API with `@rest_controller`, binding, and validation | **Chapter 4** — Your First HTTP API |
| Persisted with a framework `Repository` over SQLAlchemy/SQLite | **Chapter 5** — Persistence & the Repository Pattern |
| Modelled `Money`, `Wallet`, invariants, and domain events | **Chapter 6** — Domain-Driven Design |
| Split writes and reads with commands, queries, and the bus | **Chapter 7** — CQRS: Commands & Queries |
| Published and reacted to domain events with `@event_listener` | **Chapter 8** — Domain Events & Event-Driven Architecture |
| (Next) rebuilt state from an event log | **Chapter 9** — Event Sourcing the Ledger |
| (Next) called other services and split the monolith | **Chapters 11–12** — HTTP Clients, the BFF & Sagas |
| (Next) secured, observed, and shipped it | **Chapters 14–18** — Security, Observability, Testing & Production |

When you are ready for the *why* behind all of it, turn the page to Chapter 1.

---

## Try it yourself {.exercises}

If you want to keep moving on your own first, three small extensions build directly on what you have:

1. **Add a deposit endpoint.** You already have the `FundsDeposited` event and the aggregate's `deposit` method. Add a `DepositFunds` command + handler (model them on `OpenWallet`), a `POST /{wallet_id}/deposit` route, and watch the listener's running total climb.
2. **Add a `withdraw` path** that refuses to overdraw — the aggregate's `balance >= 0` invariant should reject it, and your handler should surface that as a clean error. Add a `FundsWithdrawn` domain event that mirrors the `FundsDeposited` event shown in Listing 0.6.
3. **Write a test for the listener**, asserting that opening a wallet and depositing leaves the expected running total — proving the event path end to end.
