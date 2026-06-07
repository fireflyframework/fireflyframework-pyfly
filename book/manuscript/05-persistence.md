<span class="eyebrow">Chapter 5</span>

# Persistence & the Repository Pattern {.chtitle}

::: figure art/openers/ch05.svg | &nbsp;

Lumen has a wallet API that works — but every wallet disappears the moment you restart the process. It is time to make wallets durable.

The naïve approach is to scatter SQLAlchemy `select()` and `session.commit()` calls through the command handlers. PyFly offers something far better: a **Spring-Data-style repository layer**. You declare an interface — `class WalletRepository(Repository[WalletEntity, str])` — and the framework *implements it for you*. Full async CRUD comes for free. Query methods are derived from their **names**. Pagination, sorting, composable filters, and read projections are first-class. There is no hand-written adapter and no SQL in the application code.

This chapter rebuilds Lumen's persistence on that layer, exactly as the running sample does it: the SQLAlchemy entity, the repository with its derived and specification queries, `Page`/`Pageable`/`Sort`, projections for read views, and the transaction seam that keeps the `Wallet` aggregate intact. Everything here runs against a real SQLite file with zero external infrastructure — the sample's 41 tests are green on it.

---

## The repository, in one sentence

::: figure art/figures/05-repository.svg | Figure 5.1 — Your code depends on the repository; the framework supplies the SQLAlchemy implementation behind it.

A PyFly repository is a class that subclasses the generic `Repository[Entity, ID]` and is marked with the `@repository` stereotype. That is the whole declaration. From the two type parameters the framework learns the **entity type** and the **primary-key type**, and from there it provides a complete async data-access surface — `save`, `find_by_id`, `find_all`, `delete`, `count`, `exists`, plus pagination and specification queries — with the database `AsyncSession` injected for you.

This is the Repository pattern as Spring Data popularised it, translated to idiomatic async Python. You write *what* you want (the method) and the framework writes *how* (the SQL).

!!! spring "Spring parity"
    `Repository[T, ID]` is PyFly's `JpaRepository<T, ID>`. Subclassing it to inherit CRUD, deriving queries from method names, `Pageable`/`Page`, `Specification`, and interface projections are all carried over almost name-for-name from Spring Data JPA. If you have written a Spring `interface OrderRepository extends JpaRepository<Order, UUID>`, you already know the shape of this chapter.

---

## The entity: one row per wallet

Before a repository can store anything, it needs an **entity** — the on-disk shape of a wallet, one flat row per aggregate. PyFly entities are ordinary SQLAlchemy 2.0 models built on a declarative base the framework exports:

::: listing lumen/models/entities/v1/wallet_orm.py | Listing 5.1 — WalletEntity: the SQLAlchemy persistence row
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy import Base


class WalletEntity(Base):
    """One persisted wallet row, keyed by the aggregate's string id."""

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

The `Mapped[T]` / `mapped_column(...)` syntax is SQLAlchemy 2.0 style: each type annotation drives both the Python attribute type and the generated column DDL, so every column has a single source of truth. Because `WalletEntity` subclasses `Base`, importing this module registers the `wallets` table in `Base.metadata`; the framework's engine lifecycle then creates it on startup.

Two design choices are worth calling out.

**Base, not BaseEntity.** PyFly ships two declarative bases. `BaseEntity` gives you a surrogate **UUID** primary key plus four audit columns (`created_at`, `updated_at`, `created_by`, `updated_by`) populated automatically — the right default for most tables. Lumen deliberately uses plain `Base` instead, because the `Wallet` aggregate already owns its identity: a string id of the form `wlt-…`. Inheriting `Base` lets the row keep that **string** primary key, so the row and the aggregate share one identity rather than the row inventing a second, surrogate one.

**Integer minor units.** Amounts live in `balance_minor` as integer cents, never as a float. Floating-point columns accumulate rounding error over millions of transactions; integer arithmetic stays exact. A balance of `2500` means €25.00 — the major-unit decimal is computed only at the edges, for display.

!!! tip "Reach for BaseEntity by default"
    Unless your aggregate owns a natural key the way `Wallet` does, prefer `class Order(BaseEntity)`. You get a UUID PK and audit columns for free, and the `AuditingEntityListener` fills `created_by`/`updated_by` from the security context on every insert and update. Lumen is the exception, not the rule.

---

## The repository: CRUD for free

Now the centrepiece. Lumen's `WalletRepository` subclasses `Repository[WalletEntity, str]` — entity type `WalletEntity`, primary-key type `str` — and is registered with `@repository`. That single declaration is enough for the framework to supply the entire CRUD surface:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.2 — WalletRepository: subclass the framework repository
from __future__ import annotations

from lumen.models.entities.v1.wallet_orm import WalletEntity
from pyfly.container import repository
from pyfly.data import Page, Pageable
from pyfly.data.relational.sqlalchemy import Repository, Specification


@repository
class WalletRepository(Repository[WalletEntity, str]):
    """CRUD + derived + specification queries for WalletEntity.

    The @repository stereotype registers this as a DI bean. The
    framework reads the entity/PK types from the
    Repository[WalletEntity, str] base and injects the shared
    AsyncSession.
    """

    # (query methods follow — see the next sections)
:::

There is no `__init__`, no SQL, and no adapter class. With just that declaration, any handler that injects a `WalletRepository` can already call:

| Method                          | Returns        | What it does                              |
|---------------------------------|----------------|-------------------------------------------|
| `save(entity)`                  | `T`            | Insert or update; **flushes** + refreshes |
| `find_by_id(id)`                | `T \| None`    | Load by primary key                       |
| `find_all(**filters)`           | `list[T]`      | All rows, optional equality filters       |
| `delete(id)`                    | `None`         | Delete by primary key (no-op if absent)   |
| `count()`                       | `int`          | Count every row in the table              |
| `exists(id)`                    | `bool`         | Whether a row with this id exists         |
| `save_all(entities)`            | `list[T]`      | Bulk insert/update                        |
| `find_all_by_ids(ids)`          | `list[T]`      | Load many rows by primary key             |
| `find_paginated(...)`           | `Page[T]`      | Paged + sorted query (see below)          |
| `find_all_by_spec(spec)`        | `list[T]`      | Rows matching a `Specification`           |
| `find_all_by_spec_paged(...)`   | `Page[T]`      | Paged + sorted `Specification` query      |

That is more than enough for most entities. Lumen adds three methods of its own on top — a derived query, a specification query, and an upsert — which the next sections build up.

### How the framework knows the types

When you write `Repository[WalletEntity, str]`, the base class's `__init_subclass__` hook inspects `__orig_bases__` at class-definition time and pulls the entity type (`WalletEntity`) and id type (`str`) out of the generic parameters. The `AsyncSession` is then supplied as an injected dependency by the relational auto-configuration. Nothing is passed manually — the type parameters *are* the wiring.

---

## Derived queries: the method name is the query

CRUD covers lookups by primary key. Real applications also need to query by other columns — "all wallets owned by this customer." In most frameworks you would write the SQL by hand. In PyFly you declare a **stub** and let the framework compile the query *from the method name*:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.3 — A derived query: declared as a stub, compiled from its name
@repository
class WalletRepository(Repository[WalletEntity, str]):

    # derived query: compiled from the method name by the post-processor
    async def find_by_owner_id(
        self, owner_id: str
    ) -> list[WalletEntity]:
        """All wallets owned by *owner_id* (derived query stub)."""
        ...
:::

The body is literally `...`. At startup a `BeanPostProcessor` — the `RepositoryBeanPostProcessor` — scans the repository, spots that `find_by_owner_id` is a stub, parses the **name** into a parsed query, and replaces the stub with a real implementation that runs `SELECT … FROM wallets WHERE owner_id = :owner_id`. Calling `await repo.find_by_owner_id("alice")` now returns exactly the rows for that owner.

The grammar is the Spring Data convention. A method name is a **prefix** followed by a **subject** built from field names, operators, connectors, and an optional ordering clause:

| Part        | Tokens                                                                   |
|-------------|--------------------------------------------------------------------------|
| Prefix      | `find_by` · `count_by` · `exists_by` · `delete_by`                       |
| Connectors  | `_and_` · `_or_`                                                          |
| Operators   | `_greater_than` · `_less_than` · `_between` · `_in` · `_like` · `_containing` · `_is_null` · `_is_not_null` |
| Ordering    | `_order_by_<field>_<asc\|desc>`                                           |

Each clause consumes the matching number of method arguments (equality and the comparisons take one; `_between` takes two; `_is_null` / `_is_not_null` take none). A few examples on a hypothetical orders repository:

```python
@repository
class OrderRepository(Repository[Order, UUID]):
    async def find_by_status(self, status: str) -> list[Order]: ...

    async def find_by_customer_id_and_status(
        self, customer_id: str, status: str
    ) -> list[Order]: ...

    async def find_by_total_greater_than(
        self, min_total: float
    ) -> list[Order]: ...

    async def find_by_total_between(
        self, low: float, high: float
    ) -> list[Order]: ...

    async def count_by_status(self, status: str) -> int: ...

    async def exists_by_customer_id(self, customer_id: str) -> bool: ...

    async def find_by_status_order_by_created_at_desc(
        self, status: str
    ) -> list[Order]: ...
```

The prefix decides the *shape* of the result: `find_by` returns a list, `count_by` returns an `int`, `exists_by` returns a `bool`, and `delete_by` issues a `DELETE` and returns the number of rows removed. You never write the SQL; you name the method and annotate the return type.

!!! tip "When a name would get silly, use @query"
    Derived names are perfect up to two or three predicates. Past that they become unreadable. For anything more complex, drop a `@query("SELECT w FROM WalletEntity w WHERE …")` decorator (JPQL-like, or `native=True` for raw SQL) on the stub and write the query explicitly. Same stub-plus-decorator pattern; you just supply the query text instead of encoding it in the name.

---

## Pagination: Page, Pageable, and Sort

A list endpoint should never return *every* wallet. PyFly's pagination types — `Pageable` (what page, what size, what sort), `Sort` (the ordering), and `Page[T]` (the slice plus metadata) — are inherited straight from the CRUD surface via `find_paginated`.

Lumen's `ListWallets` query handler is the whole story in three lines:

::: listing lumen/core/services/wallets/list_wallets_handler.py | Listing 5.4 — Paginating with find_paginated, then mapping the page
@query_handler
@service
class ListWalletsHandler(
    QueryHandler[ListWallets, Page[WalletDto]]
):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: ListWallets
    ) -> Page[WalletDto]:
        page = await self._repository.find_paginated(
            pageable=query.pageable
        )
        return page.map(entity_to_dto)
:::

`find_paginated(pageable=…)` does three things in one call: it counts the total number of matching rows, applies the `Pageable`'s sort, and slices the result with `LIMIT`/`OFFSET`. It hands back a `Page[WalletEntity]`. The handler then calls `page.map(entity_to_dto)` to turn each row into a `WalletDto` **without losing the pagination metadata** — `.map` carries `total`, `page`, `size`, and the rest across to the new page.

A `Page[T]` exposes everything a client needs to render a pager:

| Member          | Meaning                                  |
|-----------------|------------------------------------------|
| `items`         | The rows on this page (`list[T]`)        |
| `total`         | Total matching rows across all pages     |
| `page`          | Current page number (1-based)            |
| `size`          | Maximum items per page                   |
| `total_pages`   | `ceil(total / size)`                     |
| `has_next`      | Whether a next page exists               |
| `has_previous`  | Whether a previous page exists           |
| `map(fn)`       | Transform items, preserving metadata     |

The `Pageable` itself is built at the edge — the controller turns `?page=&size=` query params into a `Pageable` with a shared newest-first `Sort`:

::: listing lumen/web/controllers/wallet_controller.py | Listing 5.5 — Building a Pageable from query params (controller)
#: Newest-first ordering shared by the list endpoints.
_NEWEST_FIRST = Sort.by("created_at").descending()


@get_mapping("")
async def list_wallets(
    self, page: QueryParam[int] = 1, size: QueryParam[int] = 20
) -> PageDto[WalletDto]:
    """A page of wallets, newest first."""
    result = await self._queries.query(
        ListWallets(pageable=Pageable.of(page, size, _NEWEST_FIRST))
    )
    return PageDto.from_page(result)
:::

`Sort.by("created_at").descending()` names the column and the direction; `Pageable.of(page, size, sort)` packages it with the page coordinates. The handler returns a framework `Page`, and the controller folds it into a serialisable `PageDto` — a plain Pydantic mirror of the page — so `GET /api/v1/wallets?page=1&size=20` returns JSON like `{"items": [...], "total": 42, "page": 1, "total_pages": 3, "has_next": true, ...}`.

---

## Specifications: composable, reusable filters

Derived queries answer fixed questions. Sometimes you want a **reusable predicate** you can compose at the call site — "wallets with at least this balance," combined freely with other conditions. That is what a `Specification` is: a small object wrapping a `WHERE` fragment, composable with `&`, `|`, and `~`.

Lumen defines one as a module-level factory and uses it in a `find_rich` method:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.6 — A Specification factory and a method that runs it paged
def balance_at_least(min_minor: int) -> Specification[WalletEntity]:
    """Wallets whose balance is at least *min_minor*.

    Returned as a Specification, so it composes via & / | / ~ and
    runs through find_all_by_spec / find_all_by_spec_paged.
    """
    return Specification(
        lambda root, q: q.where(root.balance_minor >= min_minor)
    )


@repository
class WalletRepository(Repository[WalletEntity, str]):

    async def find_rich(
        self, min_minor: int, pageable: Pageable
    ) -> Page[WalletEntity]:
        """A page of wallets with balance >= min_minor."""
        return await self.find_all_by_spec_paged(
            balance_at_least(min_minor), pageable
        )
:::

A `Specification` wraps a callable `(root, q) -> q` — given the entity class (`root`) and a SQLAlchemy `Select`, it returns the statement with a predicate added. `balance_at_least(1000)` yields the predicate `balance_minor >= 1000`. Because specifications compose with Python operators, you can build arbitrarily complex filters from small pieces:

```python
rich = balance_at_least(1000)
in_eur = Specification(
    lambda root, q: q.where(root.currency == "EUR")
)
rich_eur = rich & in_eur          # AND
rich_or_eur = rich | in_eur       # OR
not_rich = ~rich                  # NOT
```

You run a specification two ways. `find_all_by_spec(spec)` returns every matching row as a list; `find_all_by_spec_paged(spec, pageable)` applies the predicate, counts the matches, sorts, and slices — returning a `Page[T]`. `find_rich` uses the paged form, so the rich-wallets endpoint is itself paginated. The handler mirrors the list handler exactly, mapping rows to DTOs:

::: listing lumen/core/services/wallets/list_rich_wallets_handler.py | Listing 5.7 — The rich-wallets handler runs the Specification path
@query_handler
@service
class ListRichWalletsHandler(
    QueryHandler[ListRichWallets, Page[WalletDto]]
):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: ListRichWallets
    ) -> Page[WalletDto]:
        page = await self._repository.find_rich(
            query.min_minor, query.pageable
        )
        return page.map(entity_to_dto)
:::

`GET /api/v1/wallets/rich?min_minor=1000&page=1&size=20` now returns a page of wallets at or above €10.00, newest first.

!!! note "Filters without lambdas"
    For the common case — equality and a handful of comparisons — you don't even need to write a lambda. `FilterOperator.gte("balance_minor", 1000) & FilterOperator.eq("currency", "EUR")` produces the same composable `Specification` from static factory methods, and `FilterUtils.by(currency="EUR")` builds one from keyword arguments (Query-by-Example). Lumen uses an explicit lambda here because the intent reads clearly; both styles produce a `Specification` you can pass to the same repository methods.

---

## Projections: read only the columns you need

The balance endpoint does not need the whole row — just the id, currency, and a computed balance. PyFly supports **interface projections**, Spring Data's idea of declaring the subset of fields a read-view wants and letting the framework copy exactly those.

A projection is a class marked `@projection`. In Lumen it is a concrete dataclass:

::: listing lumen/interfaces/dtos/v1/balance_dto.py | Listing 5.8 — BalanceView: a @projection of just the balance fields
from dataclasses import dataclass

from pyfly.data import projection


@projection
@dataclass
class BalanceView:
    """Projection: just the fields the balance view needs.

    id, currency and balance_minor are copied straight from the
    WalletEntity; balance is a computed major-unit decimal supplied
    by a registered transform on the mapper.
    """

    id: str
    currency: str
    balance_minor: int
    balance: float
:::

The mapper reads those four fields off a `WalletEntity` and constructs the view. Three (`id`, `currency`, `balance_minor`) are copied straight across; the fourth (`balance`, the major-unit decimal) is supplied by a transform registered on the mapper:

::: listing lumen/core/mappers/wallet_mapper.py | Listing 5.9 — Registering and running the projection via Mapper
from pyfly.data import Mapper

_mapper = Mapper()
_mapper.register_projection(
    WalletEntity,
    BalanceView,
    transforms={"balance": lambda e: round(e.balance_minor / 100, 2)},
)


def entity_to_balance_dto(entity: WalletEntity) -> BalanceDto:
    """Project a row onto the balance DTO via the projection."""
    view = _mapper.project(entity, BalanceView)
    return BalanceDto(
        id=view.id,
        currency=Currency(view.currency),
        balance_minor=view.balance_minor,
        balance=view.balance,
    )
:::

`Mapper.project(entity, BalanceView)` reads only the declared fields, applies the `balance` transform, and returns a `BalanceView`. The query handler then loads the row by id and projects it:

::: listing lumen/core/services/wallets/get_balance_handler.py | Listing 5.10 — The balance read handler: find by id, then project
@query_handler
@service
class GetBalanceHandler(QueryHandler[GetBalance, BalanceDto | None]):
    def __init__(self, repository: WalletRepository) -> None:
        super().__init__()
        self._repository = repository

    async def do_handle(  # type: ignore[override]
        self, query: GetBalance
    ) -> BalanceDto | None:
        entity = await self._repository.find_by_id(query.wallet_id)
        return (
            entity_to_balance_dto(entity)
            if entity is not None
            else None
        )
:::

!!! warning "A projection must be instantiable"
    Spring lets a projection be a bare interface and returns a runtime proxy. Python has no such proxy, so a PyFly projection must be a **concrete** type the mapper can construct — here, a `@dataclass`. Marking a *Protocol* `@projection` will not work: a Protocol cannot be instantiated, and `Mapper.project` has nothing to build. Use a dataclass (or any plain class with matching fields) and you are safe.

---

## Transactions and the aggregate seam

The repository surface is clean — but two honest subtleties decide whether your writes actually survive. Both come from how the framework manages the session, and Lumen handles both deliberately.

### save() flushes; it does not commit

This is the single most important thing to understand about the data layer. The framework uses **one shared `AsyncSession`**, and `Repository.save()` calls `session.add()` followed by `session.flush()` and `session.refresh()` — it **flushes**, making the write visible *within* the current session, but it never **commits**. If nothing commits, the write is rolled back when the session closes and the wallet does not survive a restart.

The commit happens at the **unit-of-work boundary**, and you declare that boundary with `@transactional()`. A handler that writes decorates its `do_handle` with `@transactional()`, injects the `async_sessionmaker` as `self._session_factory`, and the decorator opens a unit of work, swaps that transactional session onto the repository for the call, **commits on success**, and rolls back on failure:

::: listing lumen/core/services/wallets/open_wallet_handler.py | Listing 5.11 — A write handler: @transactional() commits the unit of work
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
        # @transactional resolves the unit-of-work session from here.
        self._session_factory = session_factory

    @transactional()
    async def do_handle(  # type: ignore[override]
        self, command: OpenWallet
    ) -> str:
        wallet_id = f"wlt-{uuid4()}"
        wallet = Wallet.open(
            wallet_id=wallet_id,
            owner_id=command.owner_id,
            currency=command.currency,
        )
        await self._repository.upsert(to_entity(wallet))

        await publish_domain_events(
            self._events, wallet.clear_events()
        )
        return wallet_id
:::

`@transactional()` (imported from `pyfly.data.relational.sqlalchemy`) resolves the `async_sessionmaker` from `self._session_factory`, runs the body inside a `session.begin()` block, and commits at the end. Drop the decorator and the `upsert` would only flush — the wallet would never reach disk. The read handlers earlier in this chapter need no `@transactional`: a read makes no changes to commit.

### upsert, not save, for an aggregate that owns its id

Notice the handler calls `self._repository.upsert(...)`, not `save(...)`. That is the second subtlety. The framework's `save()` issues `session.add()`, which SQLAlchemy treats as a **pending INSERT**. But the `Wallet` aggregate generates its *own* primary key up front (`wlt-…`), so by the time a deposit or withdrawal persists an already-loaded wallet, a row with that id already exists — and a second `INSERT` on the same primary key raises `IntegrityError`.

The fix is `session.merge`, which inserts when the id is new and updates when it already exists. Lumen wraps it in an `upsert` convenience method:

::: listing lumen/models/repositories/wallet_repository.py | Listing 5.12 — upsert: one call for both INSERT and UPDATE
@repository
class WalletRepository(Repository[WalletEntity, str]):

    async def upsert(self, entity: WalletEntity) -> WalletEntity:
        """Insert *entity* or update the existing row with the same id.

        Uses session.merge so a freshly-mapped entity carrying the
        aggregate's id persists whether or not a row already exists —
        the aggregate owns its primary key, so identity is never
        ambiguous. Flushes so the write is visible in the current
        unit of work; the surrounding @transactional commits it.
        """
        session = self._require_session()
        merged = await session.merge(entity)
        await session.flush()
        return merged
:::

`_require_session()` is the inherited accessor that returns the active session (the transactional one, once `@transactional` has swapped it in). `merge` keys on the primary key, so both the first write (open) and every later write (deposit, withdraw) take the same code path with no `IntegrityError`. For entities whose ids are database-generated, `save` is the natural choice; for an aggregate that owns its id, `upsert` is.

### The aggregate ↔ entity mapper seam

There is one more boundary, and it is a feature, not an accident. Lumen keeps two distinct types:

- **`Wallet`** — the DDD *aggregate root* from Chapter 6. It owns the `balance >= 0` invariant, exposes intent-revealing methods (`open`, `deposit`, `withdraw`), and raises domain events. It knows nothing about SQLAlchemy.
- **`WalletEntity`** — the *persistence row*. It is a flat SQLAlchemy model with columns and no behaviour.

A small mapper bridges them — one pure function each way:

::: listing lumen/core/mappers/wallet_mapper.py | Listing 5.13 — The aggregate ↔ row mapper
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

The write side calls `to_entity` before `upsert`; the read side either rehydrates with `to_aggregate` (when a command needs the rich aggregate) or projects straight to a DTO (when a query only needs data). Keeping the row separate from the aggregate means persistence concerns — column types, nullability, the merge dance — never leak into the domain model, and the domain's invariants never constrain the table schema. The repository stores rows; the mapper is the seam that keeps the aggregate pure.

!!! note "Rehydration skips the factory"
    `to_aggregate` calls the `Wallet` **constructor** directly, never the `Wallet.open` factory. The factory is for *new* wallets: it validates inputs and raises a `WalletOpened` event. A row loaded from the database already represents a valid, committed wallet — re-running the factory would re-fire that event and re-check rules that passed long ago. The constructor sets fields quietly, producing a `Wallet` indistinguishable from a freshly opened one but with no spurious events.

---

## Turning it on

Activating the relational layer is configuration, not code. Lumen's `pyfly.yaml` carries a `data.relational` block:

::: listing pyfly.yaml | Listing 5.14 — Relational data layer configuration
pyfly:
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///./lumen.db"
      ddl-auto: create
:::

`enabled: true` activates the relational auto-configuration, which builds the async SQLAlchemy engine and the `async_sessionmaker`, registers the `AsyncSession` and `session_factory` beans the repository and handlers inject, and installs the `RepositoryBeanPostProcessor` that compiles your derived-query stubs. `url` is a standard SQLAlchemy connection string — SQLite via `aiosqlite` here for zero-infrastructure development, `postgresql+asyncpg://…` in production. `ddl-auto: create` runs `Base.metadata.create_all` on startup, so the `wallets` table (discovered because `WalletEntity` subclasses `Base`) is built automatically the first time the app boots.

The dependency footprint is tiny: `pyfly[data-relational]` pulls in `sqlalchemy[asyncio]` and `aiosqlite`, and nothing else. No database server, no driver install — which is exactly why the sample runs anywhere.

!!! tip "Schema lifecycle in production"
    `ddl-auto: create` is right for development and samples: it creates missing tables and leaves existing ones alone. In production set `ddl-auto: none` and manage the schema with a migration tool (Alembic), which generates versioned scripts from the diff between `Base.metadata` and the live database. The application code does not change — only the `ddl-auto` setting and the migration pipeline.

---

## Proving it works

Because the repository is an ordinary class, you can test it directly against a real SQLite file — no application context, no HTTP. Lumen's repository test creates a temp database, runs `Base.metadata.create_all`, and exercises the surface end to end, including the `RepositoryBeanPostProcessor` that compiles the derived query (the same processor the live `ApplicationContext` runs):

::: listing lumen/tests/test_sql_wallet_repository.py | Listing 5.15 — Testing CRUD, the derived query, and the Specification path
def _make_repo(session: AsyncSession) -> WalletRepository:
    repo = WalletRepository(WalletEntity, session)
    # Mirror the ApplicationContext: compile derived-query stubs.
    RepositoryBeanPostProcessor().after_init(repo, "walletRepository")
    return repo


@pytest.mark.asyncio
async def test_derived_find_by_owner_id(sqlite_factory) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        await repo.upsert(_entity("wlt-1", "alice", 100))
        await repo.upsert(_entity("wlt-2", "alice", 200))
        await repo.upsert(_entity("wlt-3", "bob", 300))
        await session.commit()

        owned = await repo.find_by_owner_id("alice")
        assert sorted(w.id for w in owned) == ["wlt-1", "wlt-2"]
        assert await repo.find_by_owner_id("nobody") == []


@pytest.mark.asyncio
async def test_specification_find_rich_paged_and_sorted(
    sqlite_factory,
) -> None:
    factory, _ = sqlite_factory
    async with factory() as session:
        repo = _make_repo(session)
        # age_days drives created_at for newest-first ordering.
        await repo.upsert(_entity("wlt-poor", "a", 50, age_days=3))
        await repo.upsert(_entity("wlt-mid", "b", 1000, age_days=2))
        await repo.upsert(_entity("wlt-rich", "c", 5000, age_days=1))
        await session.commit()

        # balance_minor >= 1000, newest first, page size 1.
        newest_first = Sort.by("created_at").descending()
        page = await repo.find_rich(1000, Pageable.of(1, 1, newest_first))
        assert page.total == 2  # mid + rich
        assert page.total_pages == 2
        assert page.has_next is True
        assert [w.id for w in page.items] == ["wlt-rich"]

        # The bare predicate also works through find_all_by_spec.
        rich = await repo.find_all_by_spec(balance_at_least(5000))
        assert [w.id for w in rich] == ["wlt-rich"]
:::

The first test drives the derived query: three wallets in, two owners out, and `find_by_owner_id("alice")` returns exactly the two — proof that the framework compiled `WHERE owner_id = :owner_id` from the method name. The second drives the `Specification` path: it asserts the threshold filter (`total == 2`, only mid and rich match `>= 1000`), the newest-first sort (`wlt-rich` is the newest of the two), the page metadata (`total_pages == 2`, `has_next`), and that the same `balance_at_least` predicate also runs unpaged through `find_all_by_spec`.

The fixture mirrors what the framework does at startup — build the engine, run `Base.metadata.create_all` inside a `begin()` block so the DDL commits, hand back a session factory — so the test exercises the exact table the application creates. Other tests in the same file prove `upsert` round-trips through a *fresh* engine (durability across reconnect) and that `find_paginated` counts and slices a five-wallet table correctly.

!!! spring "Spring parity"
    Constructing the repository directly against a real in-process database mirrors Spring's `@DataJpaTest` slice, which boots an H2 database and the JPA layer in isolation to test repositories without the full context. `Base.metadata.create_all` is the analogue of `spring.jpa.hibernate.ddl-auto=create`, and running `RepositoryBeanPostProcessor` by hand stands in for the Spring proxy that materialises derived queries on a `JpaRepository` at startup.

---

## What you built {.recap}

Lumen now persists wallets through PyFly's Spring-Data-style repository layer:

- **Entity** — `WalletEntity(Base)`, a SQLAlchemy 2.0 row with a string primary key (the aggregate's own id) and integer minor-unit balances.
- **Repository** — `WalletRepository(Repository[WalletEntity, str])`, marked `@repository`. The framework supplies full async CRUD (`save`, `find_by_id`, `find_all`, `delete`, `count`, `exists`, `save_all`, `find_all_by_ids`, pagination, specifications) with no hand-written adapter.
- **Derived query** — `find_by_owner_id`, declared as a `...` stub and compiled from its name by the `RepositoryBeanPostProcessor`.
- **Pagination** — `find_paginated(pageable=…)` returning a `Page[T]` with `total` / `total_pages` / `has_next`, mapped to DTOs with `Page.map`, exposed at `GET /api/v1/wallets`.
- **Specification** — `balance_at_least(n)` composed with `& | ~` and run via `find_all_by_spec_paged`, exposed at `GET /api/v1/wallets/rich`.
- **Projection** — `@projection BalanceView`, a concrete dataclass the `Mapper` projects rows onto for the balance read view.
- **Transactions** — write handlers decorated `@transactional()` (because `save`/`upsert` only *flush*), using `upsert`/`session.merge` for an aggregate that owns its id, with the aggregate ↔ entity mapper keeping the domain model pure.

You wrote interfaces and stubs; the framework wrote the SQL. That is the payoff of the repository pattern.

---

## Try it yourself {.exercises}

1. **Add a derived counter.** Declare `async def count_by_currency(self, currency: str) -> int: ...` on `WalletRepository` (body `...`). Write a test that upserts wallets in two currencies and asserts the count for each — confirming the `count_by` prefix compiles to `SELECT COUNT(*) … WHERE currency = :currency` with no SQL on your part.

2. **Compose two specifications.** Define a second factory `in_currency(code: str) -> Specification[WalletEntity]` (predicate `currency == code`), then add a repository method that runs `balance_at_least(min_minor) & in_currency(code)` through `find_all_by_spec_paged`. Test that it returns only rich wallets in the chosen currency, newest first.

3. **Trace the transaction boundary.** Temporarily change `OpenWalletHandler.do_handle` to call `self._repository.save(to_entity(wallet))` instead of `upsert`, open the same wallet twice in one test, and observe the `IntegrityError`. Restore `upsert`. Then remove the `@transactional()` decorator, open a wallet, and assert it does **not** survive a fresh-engine reconnect — proving that without the unit-of-work commit, `flush` alone is not durability.

4. **Project a different view.** Add an `@projection OwnerView` dataclass with just `id` and `owner_id`, register it on a `Mapper`, and write a handler-free test that loads a `WalletEntity` and projects it — verifying that only the two declared columns are read and the rest of the row is ignored.
