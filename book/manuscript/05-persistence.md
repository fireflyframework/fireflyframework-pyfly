<span class="eyebrow">Chapter 5</span>

# Persistence & the Repository Pattern {.chtitle}

::: figure art/openers/ch05.svg | &nbsp;

Lumen has a wallet API that works — but every wallet disappears the moment you restart the process. The `InMemoryWalletRepository` you met in Chapter 2 was the right design for getting something running quickly: it let the command handlers depend on a clean **port**, not an implementation, so you could focus on wiring and HTTP before thinking about databases.

That investment pays off now. The port is the contract, and the contract has not changed — so adding a second adapter that persists to SQLite is purely additive. This chapter shows exactly how Lumen does it. You will read the real port definition, the in-memory adapter that ships as the default, the SQLAlchemy/SQLite adapter as a swappable alternative, and the framework configuration that ties everything together. The command handlers from Chapters 2–4 stay exactly as they are.

---

## Ports and adapters: the hexagonal approach

PyFly's DI container binds types by their Python Protocol ports. Every injectable dependency has a **port** (a `@runtime_checkable` Protocol) and one or more **adapters** (concrete classes that explicitly inherit the port). The container scans for adapters at startup, resolves port-typed constructor parameters to the matching adapter, and never exposes the concrete type to the caller.

For the wallet repository that design looks like this:

::: figure art/figures/05-repository.svg | Figure 5.1 — Command handlers depend on the WalletRepository port; both adapters implement it.

The key rule is that **a `@repository` adapter must explicitly inherit its Protocol port** — duck-typing alone is not enough. If you omit the inheritance, the container cannot match the adapter to the port and raises `NoSuchBeanError` at startup. The `class Foo(MyProtocol):` line is not boilerplate; it is the registration contract.

---

## The repository port

The port is a `@runtime_checkable` Protocol that describes the four operations the core needs:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.1 — WalletRepository: the hexagonal port
from __future__ import annotations

import asyncio
import uuid
from typing import Protocol, runtime_checkable

from lumen.models.entities.v1.wallet_entity import Wallet
from pyfly.container import primary, repository


@runtime_checkable
class WalletRepository(Protocol):
    async def add(self, wallet: Wallet) -> Wallet: ...
    async def find(self, id: str) -> Wallet | None: ...
    async def remove(self, wallet: Wallet) -> None: ...
    async def next_id(self) -> str: ...
:::

Four async methods — nothing more. No SQLAlchemy, no session, no import from `pyfly.data`. A command handler that receives a `WalletRepository` can call `add`, `find`, `remove`, and `next_id` without knowing whether it is talking to a dict or a database. That boundary is worth preserving: swap the adapter and nothing in the core changes.

`@runtime_checkable` makes the Protocol usable with `isinstance` checks at runtime, which is what PyFly's container uses to verify that an adapter truly satisfies the port before registering the binding.

---

## The in-memory adapter

The first adapter is a concurrent dictionary that lives in RAM. It is marked `@primary` so the application boots on it by default — no database required:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.2 — InMemoryWalletRepository: the default @primary adapter
@primary
@repository
class InMemoryWalletRepository(WalletRepository):
    """Concurrent in-memory store keyed by wallet id.

    Marked @primary so it is the boot default even when a second
    adapter (SqlAlchemyWalletRepository) is also registered against
    the same port.
    """

    def __init__(self) -> None:
        self._store: dict[str, Wallet] = {}
        self._lock = asyncio.Lock()

    async def add(self, wallet: Wallet) -> Wallet:
        async with self._lock:
            assert wallet.id is not None
            self._store[wallet.id] = wallet
            return wallet

    async def find(self, id: str) -> Wallet | None:
        async with self._lock:
            return self._store.get(id)

    async def remove(self, wallet: Wallet) -> None:
        async with self._lock:
            if wallet.id is not None:
                self._store.pop(wallet.id, None)

    async def next_id(self) -> str:
        return f"wlt-{uuid.uuid4()}"
:::

Three things to notice. First, `InMemoryWalletRepository` **explicitly inherits `WalletRepository`** — that single `(WalletRepository)` in the class signature is what tells the container to bind this adapter to the port. Drop it and the container has no binding, the handlers fail at startup. Second, `@primary` wins over any other adapter registered against the same port. Third, the `asyncio.Lock` makes concurrent `add` and `remove` calls safe — important even in the in-memory case because ASGI servers handle requests concurrently.

!!! tip "Port-first, adapter-later"
    Starting with an in-memory adapter is the recommended PyFly workflow. Write the port, wire the handlers against the port, and build the full feature. When you need real persistence, add the SQL adapter as a second concrete class — no handler changes required.

---

## The SQLAlchemy/SQLite adapter

The second adapter persists wallets to a relational database through PyFly's SQLAlchemy data layer. It consists of two parts: a **row class** that maps the aggregate to a table, and a **repository class** that implements the port by reading and writing those rows.

### The persistence row

`WalletRow` is the on-disk shape of a wallet — one row per aggregate:

::: listing lumen/models/repositories/sql_wallet_repository.py | Listing 5.3 — WalletRow: the SQLAlchemy mapping
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.wallet_repository import WalletRepository
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Base


class WalletRow(Base):
    """The on-disk shape of a wallet — one row per aggregate.

    Inherits PyFly's Base declarative base, so the table is part of
    Base.metadata and the framework creates it on startup
    (ddl-auto=create). The primary key is the aggregate's own string
    id (wlt-...) rather than a surrogate, keeping the row and the
    aggregate in lock-step.
    """

    __tablename__ = "wallets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC)
    )
:::

`WalletRow` inherits `Base` — **not** `BaseEntity`. `Base` is PyFly's declarative base for domain-owned tables; it leaves the primary key entirely up to you. Here the PK is the aggregate's string id (`wlt-…`), which keeps the row and the object in natural sync. `BaseEntity` forces a UUID primary key plus audit columns — useful in some services, wrong here because the `Wallet` aggregate owns its own id.

The `Mapped[T]` / `mapped_column` syntax is SQLAlchemy 2.0 style: the type annotation drives both the Python attribute type and the generated DDL column type, so there is a single source of truth.

Amounts are stored as `balance_minor` — integer minor units (cents). Floating-point columns lose precision over millions of transactions; integer arithmetic is exact. A `Money(2500, Currency.USD)` value is stored as `2500` and means $25.00.

### The repository adapter

The adapter explicitly inherits the port and takes an `AsyncSession` in its constructor:

::: listing lumen/models/repositories/sql_wallet_repository.py | Listing 5.4 — SqlAlchemyWalletRepository: the relational adapter
@repository
class SqlAlchemyWalletRepository(WalletRepository):
    """Relational adapter backed by SQLAlchemy 2.0 + SQLite (async).

    Explicitly implements the WalletRepository port so the DI container
    binds the port to it. Not marked @primary — InMemoryWalletRepository
    keeps that role — so the app boots on the in-memory store while this
    adapter remains selectable.

    The AsyncSession is injected by the framework's relational
    auto-configuration (pyfly.data.relational.enabled=true).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, wallet: Wallet) -> Wallet:
        """Upsert the aggregate, then commit the unit of work."""
        assert wallet.id is not None
        row = await self._session.get(WalletRow, wallet.id)
        if row is None:
            row = WalletRow(
                id=wallet.id,
                owner_id=wallet.owner_id,
                currency=wallet.currency.value,
                balance_minor=wallet.balance.amount,
                created_at=wallet.created_at,
            )
            self._session.add(row)
        else:
            row.owner_id = wallet.owner_id
            row.currency = wallet.currency.value
            row.balance_minor = wallet.balance.amount
        await self._session.commit()
        return wallet

    async def find(self, id: str) -> Wallet | None:
        """Load a wallet by id, rehydrating the aggregate from its row."""
        row = await self._session.get(WalletRow, id)
        return self._to_aggregate(row) if row is not None else None

    async def remove(self, wallet: Wallet) -> None:
        """Delete the wallet's row, then commit."""
        if wallet.id is None:
            return
        row = await self._session.get(WalletRow, wallet.id)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()

    async def next_id(self) -> str:
        return f"wlt-{uuid.uuid4()}"

    @staticmethod
    def _to_aggregate(row: WalletRow) -> Wallet:
        """Rehydrate a Wallet aggregate from a persistence row."""
        currency = Currency(row.currency)
        return Wallet(
            id=row.id,
            owner_id=row.owner_id,
            balance=Money(amount=row.balance_minor, currency=currency),
            created_at=row.created_at,
        )

    async def all_ids(self) -> list[str]:
        """List every persisted wallet id (for tests and diagnostics)."""
        result = await self._session.execute(select(WalletRow.id))
        return list(result.scalars().all())
:::

Walk through `add`. It calls `session.get` to check whether the row already exists. If not, it constructs a fresh `WalletRow` and hands it to the session with `session.add`. If the row is there, it updates the mutable columns in-place. Either way, `await session.commit()` flushes the change to the database. The `find` path runs the reverse mapping through `_to_aggregate`, reconstructing a fully functional `Wallet` aggregate from the stored columns.

Notice that `SqlAlchemyWalletRepository` is not marked `@primary`. Both adapters satisfy the same `WalletRepository` port, but `InMemoryWalletRepository` wins the default binding because it carries `@primary`. The SQL adapter is registered and resolvable; it just does not win the default race. To make the SQL adapter the boot default, move `@primary` from the in-memory class to this one.

!!! spring "Spring parity"
    This two-adapter design maps directly to Spring Data JPA's port/implementation split. The `WalletRepository` Protocol is the equivalent of a `JpaRepository<Wallet, String>` interface; `InMemoryWalletRepository` is the test-double / fake; `SqlAlchemyWalletRepository` is the JPA implementation. `@primary` maps to `@Primary` in Spring — exactly the same semantics: mark one bean to win when multiple beans satisfy the same dependency.

---

## Rehydration: aggregate from row

The `_to_aggregate` mapping deserves a closer look. The `Wallet` aggregate enforces invariants — `balance >= 0`, currency consistency — through its constructor and methods. Those checks must not re-fire when loading an already-valid wallet from the database: the database row reflects a state the aggregate already accepted.

PyFly's standard approach is to call the aggregate's constructor directly rather than going through the factory method (`Wallet.open`). The constructor sets fields without raising domain events or checking business rules beyond basic type safety. The factory method `Wallet.open` is for *new* wallets; the constructor is for rehydration:

```python
return Wallet(
    id=row.id,
    owner_id=row.owner_id,
    balance=Money(amount=row.balance_minor, currency=currency),
    created_at=row.created_at,
)
```

The resulting `Wallet` is indistinguishable from one that was just created in memory — same `balance`, same `currency`, same `owner_id` — but no `WalletOpened` event was raised, because opening already happened in the past.

---

## Enabling the relational stack

Two configuration changes activate the SQLAlchemy adapter.

### pyproject.toml — add the data-relational extra

::: listing pyproject.toml | Listing 5.5 — Adding pyfly[data-relational] to project dependencies
[project]
dependencies = [
    # data-relational ships SQLAlchemy 2 (async) + aiosqlite
    "pyfly[cli,web,data-relational]",
    "httpx>=0.27",
    "pydantic>=2.5",
]
:::

`pyfly[data-relational]` pulls in `sqlalchemy[asyncio]` and `aiosqlite`. Those two packages are the entire dependency footprint for SQLite persistence — no database server, no separate driver install. The lumen sample runs with zero external infrastructure for exactly this reason.

### pyfly.yaml — configure the relational layer

::: listing pyfly.yaml | Listing 5.6 — Relational data layer configuration in pyfly.yaml
pyfly:
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

`enabled: true` activates PyFly's `EngineLifecycle` bean, which builds the async SQLAlchemy engine and session factory on startup. `url` is the standard SQLAlchemy connection string — SQLite with aiosqlite for development, a PostgreSQL URL (`postgresql+asyncpg://…`) for production. `ddl-auto: create` tells the framework to call `Base.metadata.create_all` on startup, creating any missing tables automatically. The `WalletRow` table is discovered because `WalletRow` inherits `Base` — no further registration needed.

!!! tip "Schema lifecycle"
    `ddl-auto: create` is appropriate for development and for sample applications like Lumen. It creates the schema if it does not exist and leaves existing tables untouched. For production services you would set `ddl-auto: none` and manage the schema with a migration tool such as Alembic, which generates versioned scripts from the difference between `Base.metadata` and the live schema.

The full `pyfly.yaml` for Lumen also configures CQRS, EDA, event sourcing, and observability — the relational block is only one section among several. Here is the complete file for reference:

::: listing pyfly.yaml | Listing 5.7 — Complete pyfly.yaml for the Lumen sample
pyfly:
  application:
    name: lumen
    version: 1.0.0
  banner:
    mode: console
  web:
    port: 8080
  observability:
    metrics:
      enabled: true
    tracing:
      enabled: false
  cqrs:
    enabled: true
  transactional:
    enabled: true
    persistence:
      provider: in-memory
  eventsourcing:
    enabled: true
  cache:
    provider: in-memory
  eda:
    provider: memory
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

---

## Two adapters, one port: what the container does

When the application starts, PyFly's container scans all packages declared in `pyfly.yaml`. It finds two classes annotated with `@repository` that both inherit `WalletRepository`:

1. `InMemoryWalletRepository(WalletRepository)` — marked `@primary`
2. `SqlAlchemyWalletRepository(WalletRepository)` — not marked `@primary`

Both are registered. When a command handler requests a `WalletRepository` in its constructor, the container resolves the `@primary` adapter — `InMemoryWalletRepository` — because there are two candidates and primary wins ties. The `SqlAlchemyWalletRepository` is registered and available by name or type for explicit resolution; it simply does not win the default binding.

The command handlers never change. `OpenWalletHandler`, `DepositFundsHandler`, `WithdrawFundsHandler` all receive a `WalletRepository` in their constructors:

```python
class OpenWalletHandler(CommandHandler[OpenWallet, str]):
    def __init__(
        self, repository: WalletRepository, events: EventPublisher
    ) -> None:
        super().__init__()
        self._repository = repository
        self._events = events
```

That single `WalletRepository` annotation is the entire persistence contract from the handler's perspective. Whether it resolves to a dictionary or a database file is decided at startup by `@primary` — not by the handler.

---

## Testing the SQL adapter directly

Because the SQL adapter satisfies the same port, you can test it in isolation without starting the full application. The test creates a temporary SQLite database, builds `Base.metadata`, and exercises the adapter's full lifecycle:

::: listing lumen/tests/test_sql_wallet_repository.py | Listing 5.8 — SQLite adapter test: open, deposit, withdraw, prove persistence
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lumen.interfaces.enums.v1.currency import Currency
from lumen.models.entities.v1.money import Money
from lumen.models.entities.v1.wallet_entity import Wallet
from lumen.models.repositories.sql_wallet_repository import (
    SqlAlchemyWalletRepository,
)
from pyfly.data.relational.sqlalchemy import Base


@pytest_asyncio.fixture
async def sqlite_session(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
    """Temp-file SQLite engine + session factory, schema created.

    Mirrors what PyFly's EngineLifecycle does at startup: build the
    async engine and run Base.metadata.create_all. Yields the session
    factory and the database URL so the test can reconnect later.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'wallets.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, db_url
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_flow_persists_through_sqlite_adapter(
    sqlite_session: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, db_url = sqlite_session

    async with factory() as session:
        repo = SqlAlchemyWalletRepository(session=session)

        wallet_id = await repo.next_id()
        wallet = Wallet.open(
            wallet_id, owner_id="owner-42", currency=Currency.USD
        )
        await repo.add(wallet)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.deposit(Money(2500, Currency.USD))
        await repo.add(loaded)

        loaded = await repo.find(wallet_id)
        assert loaded is not None
        loaded.withdraw(Money(1000, Currency.USD))
        await repo.add(loaded)

        got = await repo.find(wallet_id)
        assert got is not None
        assert got.owner_id == "owner-42"
        assert got.currency is Currency.USD
        assert got.balance == Money(1500, Currency.USD)

    # Prove persistence: reconnect with a brand-new engine/session
    fresh_engine = create_async_engine(db_url)
    fresh_factory = async_sessionmaker(fresh_engine, expire_on_commit=False)
    try:
        async with fresh_factory() as fresh_session:
            fresh_repo = SqlAlchemyWalletRepository(session=fresh_session)
            persisted = await fresh_repo.find(wallet_id)
            assert persisted is not None
            assert persisted.balance == Money(1500, Currency.USD)
            assert persisted.owner_id == "owner-42"
            assert await fresh_repo.all_ids() == [wallet_id]
    finally:
        await fresh_engine.dispose()
:::

The test proves two things. Within the first session it exercises the full lifecycle — open, deposit, withdraw, read back — verifying that `add` behaves as an upsert and that `find` returns a properly rehydrated aggregate with the expected balance. Then it opens a completely independent engine with a fresh session and loads the same wallet again. If the data survived the reconnect, the adapter is actually writing to disk and the rehydration logic is correct.

The fixture mirrors exactly what PyFly's `EngineLifecycle` does at application startup: it creates the engine, runs `Base.metadata.create_all` inside a `begin()` context (so the DDL is committed), and hands back a session factory. Your tests therefore exercise the same table structure the application creates in production.

!!! spring "Spring parity"
    `Base.metadata.create_all` is the Python equivalent of `spring.jpa.hibernate.ddl-auto=create`. The test fixture pattern — build a real in-process database and test the repository directly — maps to Spring's `@DataJpaTest` slice, which spins up an H2 in-memory database and the JPA layer in isolation. Both approaches verify the adapter without starting the full application context.

---

## What you built {.recap}

Lumen now has two repository adapters behind a single port.

The port (`WalletRepository`) is a `@runtime_checkable` Protocol — four async method signatures, nothing else. The first adapter (`InMemoryWalletRepository`) is a concurrent dictionary, marked `@primary` so the application boots on it with zero external infrastructure. The second adapter (`SqlAlchemyWalletRepository`) maps the `Wallet` aggregate onto a `WalletRow(Base)` table using SQLAlchemy 2.0 `Mapped`/`mapped_column` syntax, stores amounts as integer minor units, and commits after every write. Both adapters explicitly inherit `WalletRepository` — the registration contract that lets the container bind them to the port. `ddl-auto: create` in `pyfly.yaml` tells the framework to create the schema from `Base.metadata` on startup; no migration tool is needed for a sample that starts fresh.

The command handlers never changed. That is the hexagonal payoff.

---

## Try it yourself {.exercises}

1. **Swap to the SQL adapter.** Move `@primary` from `InMemoryWalletRepository` to `SqlAlchemyWalletRepository`. Start the application with `pyfly run` and open a wallet with a `POST /wallets` request. Stop and restart the process, then call `GET /wallets/{id}` — the wallet should still exist because it was written to `lumen.db`. Move `@primary` back when you are done.

2. **Add a `find_by_owner` method to the port.** Add `async def find_by_owner(self, owner_id: str) -> list[Wallet]: ...` to the `WalletRepository` Protocol. Implement it in `InMemoryWalletRepository` by filtering `self._store.values()`. Implement it in `SqlAlchemyWalletRepository` using `select(WalletRow).where(WalletRow.owner_id == owner_id)`. Write a test that opens two wallets with the same owner and one with a different owner, then asserts that `find_by_owner` returns exactly the two.

3. **Verify integer minor units.** Open a wallet and deposit `Money(1050, Currency.EUR)` (€10.50) through the in-memory adapter. Check `wallet.balance.amount == 1050` and `wallet.balance.major_units == 10.5`. Then repeat through `SqlAlchemyWalletRepository` against a real SQLite file: after a deposit of `1050` and a withdrawal of `50`, assert `balance.amount == 1000` and that the value survives a reconnect.
