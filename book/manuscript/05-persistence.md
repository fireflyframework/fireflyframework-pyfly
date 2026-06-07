<span class="eyebrow">Chapter 5</span>

# Persistence & the Repository Pattern {.chtitle}

::: figure art/openers/ch05.svg | &nbsp;

Lumen has a wallet API that works — but every wallet disappears the moment you restart the process. The `InMemoryWalletRepository` you introduced in Chapter 2 was the right design for getting something running quickly: it let `WalletService` depend on a clean port, not an implementation, so you could focus on wiring and HTTP before thinking about databases.

That investment pays off now. The port is the contract, and the contract has not changed — so swapping the in-memory store for PostgreSQL is purely additive. This chapter makes that swap. You will map a `WalletEntity` to a database table with SQLAlchemy, build a real `WalletRepository` with typed CRUD methods, derived queries, and pagination, and then evolve the schema safely with a versioned Alembic migration. The controller and service from Chapters 2–4 stay exactly as they are.

---

## Entities: mapping your data

Before you can read or write a row, you need to tell SQLAlchemy what that row looks like. In PyFly, that description is a plain Python class — the **entity** — that extends `BaseEntity` from `pyfly.data.relational.sqlalchemy`. The entity is the bridge between your Python objects and a relational table, and `BaseEntity` does the repetitive parts for you.

Specifically, `BaseEntity` is an abstract SQLAlchemy `DeclarativeBase` that provides five audit columns you would otherwise copy-paste onto every table:

| Field | Column type | Description |
|---|---|---|
| `id` | Primary key (`UUID`) | Auto-generated UUID v4 |
| `created_at` | `DateTime(tz=True)` | Set automatically on insert |
| `updated_at` | `DateTime(tz=True)` | Set on insert, updated on every save |
| `created_by` | `String(255)` | Creator identifier (from `SecurityContext`, nullable) |
| `updated_by` | `String(255)` | Updater identifier (nullable) |

`BaseEntity` is abstract — no table is created for it. Your entity inherits those five columns and adds only the business-specific ones:

::: listing lumen/wallet_entity.py | Listing 5.1 — WalletEntity: mapping the wallets table
from sqlalchemy import String, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import BaseEntity


class WalletEntity(BaseEntity):
    __tablename__ = "wallets"

    owner_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    balance: Mapped[float] = mapped_column(Numeric(precision=18, scale=6), default=0.0)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
:::

Three things to notice here. First, `WalletEntity` inherits `id`, `created_at`, `updated_at`, `created_by`, and `updated_by` without writing a single column definition — those five lines are invisible but present on every entity in the codebase. Second, the business columns use SQLAlchemy's `Mapped[T]` annotation syntax: the type hint drives both the Python attribute type and the generated DDL, so there is a single source of truth. Third, `owner_id` carries `index=True` — a deliberate choice because "find all wallets for this owner" is the most common query, and the index makes it fast regardless of table size.

SQLAlchemy's async engine, session factory, and audit listener (which populates `created_by` / `updated_by` from the security context) are all wired by auto-configuration. You declare the entity; the framework handles the plumbing.

To enable the SQLAlchemy adapter, add two keys to `pyfly.yaml`:

::: listing pyfly.yaml | Listing 5.2 — Enabling the relational adapter in pyfly.yaml
pyfly:
  data:
    relational:
      enabled: true
      url: "postgresql+asyncpg://lumen:secret@localhost:5432/lumen"
      echo: false
      ddl-auto: "none"
:::

`ddl-auto: none` tells PyFly not to touch the schema at startup — you want full control through migrations, which you will write at the end of this chapter. During very early development, `ddl-auto: create` is a faster alternative that creates (or drops and recreates) tables on every boot. Switch it back to `none` before you write your first migration.

!!! tip "SQLite for development"
    Replace the `url` with `sqlite+aiosqlite:///lumen.db` for a zero-dependency local setup. SQLite works with every feature in this chapter; just swap back to PostgreSQL before staging.

---

## Repositories

The service layer should not know whether wallets live in PostgreSQL, MongoDB, or a spreadsheet. That is not a platitude — it is a practical guarantee: if `WalletService` depends on the database directly, you cannot test it without a database, and you cannot change the database without touching the service.

A **repository** is the pattern that enforces this separation. It is the sole gateway between your service layer and the database: the service calls methods like `save`, `find_by_id`, and `find_paginated`; the repository translates those calls into SQL and back. In PyFly's hexagonal design your service declares its dependency as the *port* (`RepositoryPort[T, ID]`), and the SQLAlchemy adapter fulfils the port. The two never meet directly.

### The Repository[T, ID] class

`Repository[T, ID]` from `pyfly.data.relational.sqlalchemy` is the concrete SQLAlchemy implementation. Subclass it with your entity and ID types and annotate the subclass with `@repository`. The framework resolves the entity type from the generic parameters at class-definition time and injects an async `AsyncSession` from the session factory automatically:

::: listing lumen/wallet_repository.py | Listing 5.3 — WalletRepository: the SQLAlchemy-backed concrete repository
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository

from lumen.wallet_entity import WalletEntity


@repository
class WalletRepository(Repository[WalletEntity, str]):
    pass
:::

Three lines. No `__init__`, no session wiring, no SQL. The base class provides everything from the `RepositoryPort[T, ID]` protocol out of the box:

| Method | Return type | Description |
|---|---|---|
| `save(entity)` | `T` | Insert or update; flushes and refreshes |
| `find_by_id(id)` | `T \| None` | Find by primary key |
| `find_all(**filters)` | `list[T]` | Find all, optionally filtered by column values |
| `delete(id)` | `None` | Delete by primary key; no-op if not found |
| `count()` | `int` | Count all rows |
| `exists(id)` | `bool` | Check whether a row with this ID exists |
| `find_paginated(page, size, pageable)` | `Page[T]` | Paginated query with optional sorting |
| `find_all_by_spec(spec)` | `list[T]` | Find all matching a Specification |
| `find_all_by_spec_paged(spec, pageable)` | `Page[T]` | Paginated query with Specification + sorting |

### The port keeps your service clean

`WalletRepository(Repository[WalletEntity, str])` satisfies `RepositoryPort[WalletEntity, str]`. That means `WalletService` can declare its dependency as the port and receive the concrete repository through constructor injection — without importing SQLAlchemy:

::: figure art/figures/05-repository.svg | Figure 5.1 — Your code depends on the repository port; the SQLAlchemy adapter fulfils it.

```python
from pyfly.data import RepositoryPort


class WalletService:
    def __init__(self, repo: RepositoryPort[WalletEntity, str]) -> None:
        self._repo = repo
```

`WalletService` written this way is completely database-agnostic. Swap `WalletRepository` for a MongoDB adapter, a test-double, or any future storage technology, and the service needs no changes — not even a recompile.

!!! spring "Spring parity"
    `Repository[T, ID]` maps directly to Spring Data JPA's `JpaRepository<T, ID>`, which itself extends `CrudRepository<T, ID>` and `PagingAndSortingRepository<T, ID>`. The same pattern applies: subclass the generic base with your entity and key types, annotate with `@repository` (≈ `@Repository` in Spring), and let the container wire everything. The method names — `save`, `findById`, `findAll`, `delete`, `count`, `existsById` — map one-to-one (camelCase vs snake_case aside).

---

## Derived queries

Once you have a working repository, patterns emerge quickly. You find yourself writing `SELECT * FROM wallets WHERE owner_id = ?` in one place, `SELECT COUNT(*) FROM wallets WHERE owner_id = ?` in another, and a handful of similar one-liners throughout the codebase. They are not complex — they are just noise that obscures what the code is actually doing.

PyFly generates those query bodies from the method name itself, following the same naming convention as Spring Data. You declare a **stub method** on your repository. The `RepositoryBeanPostProcessor` inspects the class after initialization, detects the stub (a method whose body is only `...` or `pass`), parses the name through `QueryMethodParser`, compiles it into a SQLAlchemy expression, and patches the method before any code calls it. By the time your service makes the first call, the real implementation is already in place — with no runtime reflection overhead per invocation.

The naming grammar is:

```
<prefix>_<field>[_<operator>][_<connector>_<field>...][_order_by_<field>_<direction>...]
```

Four prefixes are supported: `find_by_` (returns `list[T]`), `count_by_` (returns `int`), `exists_by_` (returns `bool`), and `delete_by_` (returns the row count as `int`).

Here are the derived queries useful for Lumen, plus a `@query`-decorated custom query for a more complex lookup:

::: listing lumen/wallet_repository.py | Listing 5.4 — Derived queries and a custom @query on WalletRepository
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository, query

from lumen.wallet_entity import WalletEntity


@repository
class WalletRepository(Repository[WalletEntity, str]):

    # All wallets belonging to an owner (equality operator, default)
    async def find_by_owner_id(self, owner_id: str) -> list[WalletEntity]: ...

    # Find by owner and currency together
    async def find_by_owner_id_and_currency(
        self, owner_id: str, currency: str
    ) -> list[WalletEntity]: ...

    # Wallets with a balance above a threshold, newest first
    async def find_by_balance_greater_than_order_by_created_at_desc(
        self, min_balance: float
    ) -> list[WalletEntity]: ...

    # How many wallets exist for an owner
    async def count_by_owner_id(self, owner_id: str) -> int: ...

    # Does a wallet exist for this owner/currency pair?
    async def exists_by_owner_id_and_currency(
        self, owner_id: str, currency: str
    ) -> bool: ...

    # Custom query for wallets above a balance floor, sorted by balance
    @query(
        "SELECT w FROM WalletEntity w"
        " WHERE w.owner_id = :owner_id AND w.balance >= :min_balance"
        " ORDER BY w.balance DESC"
    )
    async def find_rich_wallets(
        self, owner_id: str, min_balance: float
    ) -> list[WalletEntity]: ...
:::

Walk through what the parser does with each stub. `find_by_owner_id` decomposes to: prefix `find_by_`, field `owner_id`, operator implicit equality — the generated WHERE clause is `WHERE wallets.owner_id = :owner_id`. `find_by_balance_greater_than_order_by_created_at_desc` is longer but still mechanical: field `balance`, operator `greater_than` (mapped to `>`), then an ORDER BY clause on `created_at` descending. The method signature must supply one positional argument for each extracted field — the parser validates arity at class-load time, not at call time.

For anything the naming grammar cannot express, `@query` accepts a JPQL-like string (`FROM WalletEntity w WHERE w.field = :param`) or raw SQL when `native=True` is passed. Named parameters in the query string (`:owner_id`, `:min_balance`) are bound from the method's keyword arguments in order.

Every stub body is `...`. That is all the compiler needs — the rest is handled before the first call.

!!! tip "Longest-match operator parsing"
    The parser matches operators longest-first, so `balance_greater_than_equal` is recognised as `>=` before falling back to `>`. Append `_order_by_<field>_asc` or `_order_by_<field>_desc` to any derived query to control result ordering — the clause is parsed after the predicates and applied at the end of the SELECT.

---

## Pagination & sorting

Returning an unbounded `list` from a repository is fine for small, bounded data sets. For anything user-facing — a wallet list, a transaction history, a search result page — you cannot afford to pull every row into memory just to show the first twenty. Your API needs to accept `page` and `size` parameters and return a `Page[T]`: a frozen snapshot that carries the current-page items, the total row count, and enough metadata to drive client-side navigation controls.

The pagination vocabulary lives in `pyfly.data`:

- `Pageable` — encapsulates `page` (1-based), `size`, and a `Sort`; created with `Pageable.of(page, size, sort=...)`.
- `Sort` — an ordered list of `Order` objects; created with `Sort.by("field")` or `Sort.by("field").descending()`.
- `Page[T]` — the result type returned by `find_paginated` and `find_all_by_spec_paged`.

`Page[T]` exposes: `items` (the current-page list), `total` (rows across all pages), `page`, `size`, `total_pages`, `has_next`, `has_previous`. Call `page.map(fn)` to transform items — converting `WalletEntity` to `WalletSummary`, for example — while preserving all pagination metadata automatically.

Here is how to wire pagination into the wallet list endpoint. The controller already accepts `page` and `size` as query parameters from Chapter 4; only the service method changes:

::: listing lumen/wallet_service.py | Listing 5.5 — Paginated wallet list in WalletService
from dataclasses import dataclass

from pyfly.container import service
from pyfly.data import Page, Pageable, Sort

from lumen.wallet_entity import WalletEntity
from lumen.wallet_repository import WalletRepository


@dataclass
class WalletSummary:
    id: str
    owner_id: str
    balance: float
    currency: str


@service
class WalletService:
    def __init__(self, repo: WalletRepository) -> None:
        self._repo = repo

    async def list_wallets(
        self,
        owner_id: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> Page[WalletSummary]:
        pageable = Pageable.of(
            page=page,
            size=size,
            sort=Sort.by("created_at").descending(),
        )
        if owner_id:
            from pyfly.data.relational.sqlalchemy import FilterOperator
            spec = FilterOperator.eq("owner_id", owner_id)
            raw: Page[WalletEntity] = await self._repo.find_all_by_spec_paged(
                spec, pageable
            )
        else:
            raw = await self._repo.find_paginated(pageable=pageable)

        return raw.map(
            lambda w: WalletSummary(
                id=str(w.id),
                owner_id=w.owner_id,
                balance=float(w.balance),
                currency=w.currency,
            )
        )
:::

Here is what each piece of this method does. `Pageable.of` bundles the page number, page size, and sort order into a single value object that the repository understands. `Sort.by("created_at").descending()` means the newest wallets always appear first — a sensible default for a financial product. The `if owner_id` branch uses a `FilterOperator.eq` specification (you will read about Specifications in the next section) to scope the query to a single owner; the `else` branch queries across all owners. Either way, `raw.map(...)` converts each `WalletEntity` to the lighter `WalletSummary` dataclass, carrying forward the total count and page metadata untouched. The controller receives a fully populated `Page[WalletSummary]` without knowing anything about how the data was fetched.

!!! note "Note"
    `Pageable` validates its arguments: `page < 1` or `size < 1` raises `ValueError`. For an ad-hoc "fetch everything" query, use `Pageable.unpaged()` — the repository skips the `OFFSET` / `LIMIT` clauses and counts all matching rows.

---

## Specifications

Derived queries cover equality and simple comparisons, and they shine when the filter conditions are fixed. When you need to compose conditions *dynamically* — adding a currency filter only when the caller supplies it, combining three optional search fields from a form submission — method names become unwieldy and the combinatorial explosion of stubs is unmanageable.

`Specification` solves this by treating each predicate as a first-class value you can compose at runtime. A `Specification[T]` is a callable predicate: it receives the entity class and a SQLAlchemy `Select` statement, applies a `WHERE` clause, and returns the modified statement. You create one from a lambda or use `FilterOperator`'s factory methods, then compose with `&` (AND), `|` (OR), and `~` (NOT) — the same way you compose boolean expressions, but lazily:

::: listing lumen/wallet_service.py | Listing 5.6 — Composable Specifications for dynamic wallet search
from pyfly.data.relational.sqlalchemy import FilterOperator, Specification

from lumen.wallet_entity import WalletEntity


async def search_wallets(
    repo,
    owner_id: str | None = None,
    currency: str | None = None,
    min_balance: float | None = None,
) -> list[WalletEntity]:
    # Start with a match-everything predicate
    spec: Specification = FilterOperator.eq("currency", "USD") | (
        ~FilterOperator.eq("currency", "USD")
    )

    if owner_id:
        spec = spec & FilterOperator.eq("owner_id", owner_id)

    if currency:
        spec = spec & FilterOperator.eq("currency", currency)

    if min_balance is not None:
        spec = spec & FilterOperator.gte("balance", min_balance)

    return await repo.find_all_by_spec(spec)
:::

The pattern reads naturally: you start with a predicate that matches everything (the tautology `USD OR NOT USD`), then narrow it with `&` for each filter the caller actually provided. If `owner_id` is `None`, the owner clause is never added — not added as `WHERE owner_id IS NULL`, simply not added at all. The final `spec` is whatever combination of clauses was built, and a single `find_all_by_spec` call executes it.

`FilterOperator` covers the full set of common predicates — `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `like`, `contains`, `in_list`, `is_null`, `is_not_null`, `between` — so most search forms need no hand-written lambdas. Combine the result with `find_all_by_spec` for a simple list or `find_all_by_spec_paged` when you also need pagination.

!!! tip "Inline Specification"
    When `FilterOperator` does not cover your case, write a one-liner: `Specification(lambda root, q: q.where(root.balance > 0))`. The `root` is the SQLAlchemy mapped class; `q` is the current `Select`; return the modified statement. Composition with `&` / `|` / `~` works exactly the same way.

---

## Transactions

Every `save` and `delete` on `Repository` is already wrapped in a single flush — adequate for isolated, single-row operations. Multi-step writes are a different matter. Crediting one wallet while debiting another must either both succeed or both fail. If the credit completes and the debit then raises an exception, you have created money from nothing. If the debit completes and the credit fails, you have destroyed it. Either outcome is wrong, and neither is acceptable in a financial service.

This all-or-nothing guarantee is what a database **transaction** provides. PyFly's `@transactional` decorator handles it declaratively, so you express the intent without writing session management code. Decorate the service method, and the framework opens a session, begins a transaction, commits on success, and rolls back on any exception — including exceptions raised deep inside called methods:

::: listing lumen/wallet_service.py | Listing 5.7 — Atomic fund transfer with @transactional
from sqlalchemy.ext.asyncio import async_sessionmaker

from pyfly.container import service
from pyfly.data.relational.sqlalchemy import transactional

from lumen.wallet_entity import WalletEntity
from lumen.wallet_repository import WalletRepository


@service
class TransferService:
    def __init__(
        self,
        repo: WalletRepository,
        session_factory: async_sessionmaker,
    ) -> None:
        self._repo = repo
        self._session_factory = session_factory

    @transactional()
    async def transfer(
        self,
        from_wallet_id: str,
        to_wallet_id: str,
        amount: float,
    ) -> None:
        source = await self._repo.find_by_id(from_wallet_id)
        target = await self._repo.find_by_id(to_wallet_id)

        if source is None or target is None:
            raise ValueError("Wallet not found")
        if source.balance < amount:
            raise ValueError("Insufficient funds")

        source.balance -= amount
        target.balance += amount

        await self._repo.save(source)
        await self._repo.save(target)
        # Both saves committed atomically; any exception rolls back both
:::

Walk through the method. The two `find_by_id` calls load both wallets within the same transaction-scoped session — important, because a concurrent transfer to the same wallet must wait at the database level rather than racing in Python. The guard clauses raise `ValueError` before any mutation if either wallet is missing or the balance is insufficient; `@transactional` catches those exceptions and rolls back immediately. The two `save` calls then modify both entities; because they share the same session, they share the same transaction. Both writes commit together when the method returns normally, or both roll back if anything goes wrong after the first save.

`@transactional()` resolves `async_sessionmaker` from `self._session_factory` and automatically patches the repository instances on the service with the transaction-scoped session, so both `save` calls share the same transaction without any wiring in your code.

The decorator accepts optional `propagation` and `isolation` arguments. The default propagation is `REQUIRED`: join an existing transaction if one is active, otherwise open a new one. Use `Propagation.REQUIRES_NEW` for operations that must commit independently of the caller (audit logs, outbox events). Use `Isolation.SERIALIZABLE` for reports or transfer checks where phantom reads must be impossible.

!!! warning "All-or-nothing is the goal, not the default"
    Without `@transactional`, two `save` calls in the same method run in separate sessions. If the second save raises after the first commits, the database is left in a partial state. Wrap every multi-step write in `@transactional()` — the cost is negligible and the correctness guarantee is absolute.

---

## Evolving the schema: migrations

`ddl-auto: create` is convenient in the first hours of development, when the schema is still in flux and losing data between restarts is acceptable. It is never acceptable in staging or production, because it **drops and recreates tables on every startup**, taking all existing data with it.

The safe alternative is **migrations**: versioned scripts that describe each incremental change to the schema and can be applied forward or, when necessary, reversed. A migration knows what the schema looked like before it ran and what it should look like after. Applied in order, they bring any database — brand-new or months old — to exactly the version the code expects.

PyFly's migration support is powered by [Alembic](https://alembic.sqlalchemy.org/). The `pyfly db` commands wrap Alembic with framework-aware defaults so you rarely need to touch `alembic.ini` or `env.py` directly.

### Initialising the migration environment

Run this once in your project root:

::: listing terminal | Listing 5.8 — Initialising Alembic for Lumen
pyfly db init
:::

This creates an `alembic/` directory with Alembic's standard structure and writes a PyFly-customised `env.py` that already imports `Base.metadata` from `pyfly.data.relational.sqlalchemy`, wires `async_engine_from_config` for async database drivers (asyncpg, aiosqlite), and supports both online (live connection) and offline (SQL-script) migration modes. You do not need to configure any of this by hand.

### Generating the first migration

With `WalletEntity` defined, generate the initial migration:

::: listing terminal | Listing 5.9 — Auto-generating the wallets table migration
pyfly db migrate -m "create wallets table"
:::

Alembic compares `Base.metadata` — every entity you have defined, including all inherited `BaseEntity` columns — against the current database state and writes `upgrade` and `downgrade` functions in a new file under `alembic/versions/`. Open the file and review it before applying: Alembic is accurate for columns and primary keys, but it does not detect every naming convention for constraints and indexes. This is a five-minute habit that prevents surprises in production.

A generated migration file for the wallets table looks like this:

::: listing alembic/versions/0001_create_wallets_table.py | Listing 5.10 — Generated migration: create the wallets table
"""create wallets table

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-06-07 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("balance", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wallets_owner_id", "wallets", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_wallets_owner_id", table_name="wallets")
    op.drop_table("wallets")
:::

Notice how the migration faithfully reflects what `WalletEntity` declared: the three business columns (`owner_id`, `balance`, `currency`), the five audit columns from `BaseEntity`, the primary key, and the index on `owner_id` that you requested with `index=True`. The `downgrade` function is the exact inverse — drop the index first, then the table — so rolling back this migration leaves the database in the state it was in before `upgrade` ran.

### Applying migrations

Apply all pending migrations to bring the database to `head`:

::: listing terminal | Listing 5.11 — Applying migrations
pyfly db upgrade
:::

To apply up to a specific revision: `pyfly db upgrade abc123`. To roll back one step: `pyfly db downgrade -1`. To revert everything: `pyfly db downgrade base`.

Once the migration is applied, switch `ddl-auto` back to `none` in `pyfly.yaml` — the schema is now managed exclusively by Alembic.

!!! note "Note"
    Every time you add a column, rename a table, or introduce an index, run `pyfly db migrate -m "description"` followed by `pyfly db upgrade`. Commit the generated file in `alembic/versions/` alongside your entity changes — both go in the same pull request so the schema is never out of step with the code that reads it.

!!! spring "Spring parity"
    Alembic with `pyfly db` is the Python equivalent of Flyway or Liquibase wired into a Spring Boot application. `pyfly db migrate` generates versioned scripts the way Flyway discovers `V1__create_orders_table.sql` files — except Alembic's autogenerate derives the diff from your SQLAlchemy models rather than requiring you to write the SQL by hand. `ddl-auto: none` maps to `spring.jpa.hibernate.ddl-auto=validate` or `=none`; `ddl-auto: create` maps to `=create`.

---

## What you built {.recap}

Part II is off to a solid start.

Lumen now writes to a real database without a single line of business logic changing. You defined `WalletEntity` by extending `BaseEntity` — five audit columns for free, four business columns for the domain — and enabled the SQLAlchemy adapter with two lines in `pyfly.yaml`. `WalletRepository` subclasses `Repository[WalletEntity, str]` and provides typed CRUD, derived query methods compiled from method names at class-load time, and both list and paginated retrieval. `WalletService` depends on the repository through the `RepositoryPort` protocol and remains unaware of the database engine underneath. Multi-step writes are wrapped in `@transactional()` to guarantee atomicity: both saves commit together or neither does. The schema is versioned with Alembic migrations generated by `pyfly db migrate` — no hand-written SQL, no `ddl-auto: create` in production.

The controller and service code from Chapters 2–4 are untouched. That is the hexagonal payoff.

---

## Try it yourself {.exercises}

1. **Add a derived query for currency search.** Add `find_by_currency` to `WalletRepository` as a stub method that returns `list[WalletEntity]`. Then expose it in `WalletService.list_wallets` as an additional optional filter. Add `currency: QueryParam[str] = None` to the `list_wallets` handler in `WalletController` and verify that `GET /wallets?currency=EUR` returns only EUR wallets while `GET /wallets` still returns all wallets.

2. **Add a paged `/wallets/{owner_id}/wallets` endpoint.** Add a `GET /owners/{owner_id}/wallets` route to `WalletController` that accepts `page` and `size` query parameters and returns the `Page[WalletSummary]` from `WalletService.list_wallets`. Serialize `items`, `total`, `page`, `size`, `total_pages`, `has_next`, and `has_previous` in the response body. Verify with `GET /owners/alice/wallets?page=1&size=2` that the pagination metadata is correct when Alice has more than two wallets.

3. **Write a migration that adds a `status` column.** Add a `status: Mapped[str] = mapped_column(String(20), default="ACTIVE")` field to `WalletEntity`. Run `pyfly db migrate -m "add wallet status"` and inspect the generated file in `alembic/versions/`. Apply it with `pyfly db upgrade`. Then add a `find_by_status` derived query stub to `WalletRepository`, verify it compiles at startup, and call it from a new `WalletService.find_active_wallets` method that filters by `"ACTIVE"`.
