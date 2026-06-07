# Data Relational — SQLAlchemy Adapter

> **Package:** `pyfly.data.relational.sqlalchemy`
> **Commons:** [`pyfly.data`](data.md) — shared ports, pagination, query parsing, entity mapping
>
> This guide covers the **SQLAlchemy adapter** for relational databases. For generic data concepts shared across all adapters (repository ports, `Page`/`Pageable`/`Sort`, `QueryMethodParser`, `Mapper`, extensibility), see the [Data Module Guide](data.md). For document databases, see the [Data Document Guide](data-document.md).
>
> **Hexagonal by design:** your services depend on [`RepositoryPort[T, ID]`](data.md#repository-ports) (the port), never on `Repository[T, ID]` (the adapter). SQLAlchemy is the default relational adapter today — but the layer is designed so any relational backend (Tortoise ORM, Django ORM, etc.) can be added by implementing the same ports.

PyFly Data Relational implements the Repository pattern with Spring Data-style derived query methods, composable specifications, pagination, entity mapping, and declarative transaction management — backed by SQLAlchemy's async ORM.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Entity Definition](#entity-definition)
  - [Base (DeclarativeBase)](#base-declarativebase)
  - [BaseEntity: Audit Trail Fields](#baseentity-audit-trail-fields)
  - [Defining Your Own Entities](#defining-your-own-entities)
- [Repository Pattern](#repository-pattern)
  - [Repository Class](#repository-class)
  - [Creating a Repository](#creating-a-repository)
  - [CRUD Methods Reference](#crud-methods-reference)
- [Derived Query Methods](#derived-query-methods)
  - [Complete Derived Query Examples](#complete-derived-query-examples)
- [Custom Queries with @query](#custom-queries-with-query)
  - [JPQL-Like Syntax](#jpql-like-syntax)
  - [Native SQL](#native-sql)
  - [Return Type Inference](#return-type-inference)
  - [JPQL Transpilation Details](#jpql-transpilation-details)
- [Specifications](#specifications)
  - [Creating Specifications](#creating-specifications)
  - [Combining Specifications](#combining-specifications)
  - [Using Specifications with Repositories](#using-specifications-with-repositories)
- [FilterOperator](#filteroperator)
  - [Available Operators](#available-operators)
  - [Composing Filters](#composing-filters)
- [FilterUtils: Query by Example](#filterutils-query-by-example)
- [Pagination](#pagination)
  - [Paginated Queries](#paginated-queries)
  - [Paginated Specification Queries](#paginated-specification-queries)
- [Transaction Management](#transaction-management)
- [Read/Write Routing (Read Replicas)](#readwrite-routing-read-replicas)
- [Data Auditing](#data-auditing)
  - [AuditingEntityListener](#auditingentitylistener)
  - [How Auditing Works](#how-auditing-works)
  - [Resolving the Current User](#resolving-the-current-user)
  - [Registering the Listener](#registering-the-listener)
- [RepositoryBeanPostProcessor](#repositorybeanpostprocessor)
  - [How It Works](#how-it-works)
  - [Stub Detection](#stub-detection)
- [QueryMethodCompiler](#querymethodcompiler)
- [Complete CRUD Example](#complete-crud-example)
- [See Also](#see-also)

---

## Architecture Overview

All concrete types live in the SQLAlchemy adapter package. The namespace `pyfly.data.relational` is a pass-through and does not re-export anything.

```python
from pyfly.data.relational.sqlalchemy import (
    Base, BaseEntity,                   # SQLAlchemy entity base classes
    Repository,                         # Repository[T, ID] implementation
    Specification,                      # Composable query predicates
    FilterOperator, FilterUtils,        # Query-by-example utilities
    QueryExecutor, query,               # Custom @query decorator
    QueryMethodCompiler,                # Derived query → SQLAlchemy compiler
    RepositoryBeanPostProcessor,        # Auto-wires query methods
    reactive_transactional,             # Declarative transaction management
)
```

> **Note:** Always import concrete types from `pyfly.data.relational.sqlalchemy`. Commons types (`Page`, `Pageable`, `RepositoryPort`, etc.) are imported from `pyfly.data` — see the [Data Module Guide](data.md#import-rules).

---

## Entity Definition

### Base (DeclarativeBase)

PyFly exports a pre-configured SQLAlchemy `DeclarativeBase`:

```python
from pyfly.data.relational.sqlalchemy import Base
```

Use `Base` directly when you need SQLAlchemy entities without the built-in audit trail fields.

### BaseEntity: Audit Trail Fields

`BaseEntity` extends `Base` and provides a UUID primary key plus four audit trail columns. All domain entities should inherit from this class:

```python
from pyfly.data.relational.sqlalchemy import BaseEntity
```

**Inherited fields:**

| Field        | Type              | Column Type        | Description                          |
|--------------|-------------------|--------------------|--------------------------------------|
| `id`         | `Mapped[UUID]`    | Primary key        | Auto-generated UUID v4               |
| `created_at` | `Mapped[datetime]`| `DateTime(tz=True)`| Set automatically on insert          |
| `updated_at` | `Mapped[datetime]`| `DateTime(tz=True)`| Set on insert, updated on every save |
| `created_by` | `Mapped[str\|None]`| `String(255)`     | Creator identifier (default `None`)  |
| `updated_by` | `Mapped[str\|None]`| `String(255)`     | Updater identifier (default `None`)  |

`BaseEntity` is declared with `__abstract__ = True`, so it does not create its own database table.

### Defining Your Own Entities

Extend `BaseEntity` and declare your domain columns:

```python
from pyfly.data.relational.sqlalchemy import BaseEntity
from sqlalchemy import String, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column


class Order(BaseEntity):
    __tablename__ = "orders"

    customer_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    total: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
```

This entity will have all five inherited fields (`id`, `created_at`, `updated_at`, `created_by`, `updated_by`) plus your four custom columns.

---

## Repository Pattern

### Repository Class

The `Repository[T, ID]` class provides generic async CRUD operations for any SQLAlchemy model. The two type parameters are:

- **T** — The entity type (any SQLAlchemy model, including `BaseEntity` subclasses or plain `Base` subclasses)
- **ID** — The primary key type (e.g. `UUID`, `int`, `str`)

When you subclass `Repository[T, ID]` with concrete type parameters, the framework automatically extracts the entity type and ID type via `__init_subclass__`. The `AsyncSession` is injected by the DI container from the auto-configured `async_sessionmaker`. No explicit `__init__` is needed:

```python
from uuid import UUID
from pyfly.data.relational.sqlalchemy import Repository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class OrderRepository(Repository[Order, UUID]):
    pass

# Usage (session is injected by the container):
order = await repo.save(Order(customer_id="abc", status="PENDING"))
found = await repo.find_by_id(order.id)
```

`Repository[T, ID]` satisfies the [`RepositoryPort[T, ID]`](data.md#repository-ports) protocol, enabling hexagonal architecture where your service layer depends on the port, not the adapter.

### Creating a Repository

Subclass `Repository[T, ID]` with concrete type parameters and register it with the `@repository` stereotype:

```python
from uuid import UUID
from pyfly.data.relational.sqlalchemy import Repository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class OrderRepository(Repository[Order, UUID]):
    pass
```

For entities with integer primary keys:

```python
@repo_stereotype
class ProductRepository(Repository[Product, int]):
    pass
```

**How it works:**

1. `__init_subclass__` inspects `__orig_bases__` to extract the entity type (`Order`) and ID type (`UUID`) from the generic parameters at class definition time.
2. The `AsyncSession` is provided as an auto-configured bean by `RelationalAutoConfiguration` and injected by the container into the repository's constructor.
3. The entity type is used internally for all query operations — no need to pass it manually.

### CRUD Methods Reference

| Method                                           | Return Type  | Description                                    |
|--------------------------------------------------|--------------|------------------------------------------------|
| `save(entity)`                                   | `T`          | Insert or update; flushes and refreshes        |
| `find_by_id(id: ID)`                             | `T \| None`  | Find by primary key                            |
| `find_all(**filters)`                             | `list[T]`    | Find all, optionally filtered by column values |
| `delete(id: ID)`                                  | `None`       | Delete by primary key (no-op if not found)     |
| `count()`                                         | `int`        | Count all entities in the table                |
| `exists(id: ID)`                                  | `bool`       | Check if an entity with this ID exists         |
| `find_paginated(page, size, pageable)`            | `Page[T]`    | Paginated query with optional sorting          |
| `find_all_by_spec(spec)`                          | `list[T]`    | Find all matching a Specification              |
| `find_all_by_spec_paged(spec, pageable)`          | `Page[T]`    | Paginated query with Specification + sorting   |

**save()** calls `session.add()`, then `session.flush()` and `session.refresh()` to ensure the returned entity has all database-generated values (ID, defaults, etc.).

**find_all()** accepts keyword arguments that are translated into equality filters:

```python
orders = await repo.find_all(status="PENDING", customer_id="abc")
# Equivalent to: SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 'abc'
```

**delete()** looks up the entity first and deletes it if found. If not found, it is a no-op.

---

## Derived Query Methods

PyFly automatically generates query implementations from method names using the Spring Data naming convention. You define stub methods on your repository and the `RepositoryBeanPostProcessor` compiles them into real SQLAlchemy queries at startup.

For the full naming convention reference (prefixes, operators, connectors, ordering), see the [Data Module Guide — Derived Query Methods](data.md#derived-query-methods).

### Complete Derived Query Examples

```python
@repo_stereotype
class OrderRepository(Repository[Order, UUID]):

    # Equals (default operator)
    async def find_by_status(self, status: str) -> list[Order]: ...

    # Multiple conditions with AND
    async def find_by_customer_id_and_status(
        self, customer_id: str, status: str
    ) -> list[Order]: ...

    # Greater than
    async def find_by_total_greater_than(self, min_total: float) -> list[Order]: ...

    # Between (takes 2 arguments)
    async def find_by_total_between(self, low: float, high: float) -> list[Order]: ...

    # LIKE pattern
    async def find_by_customer_id_like(self, pattern: str) -> list[Order]: ...

    # Contains (wraps value in %)
    async def find_by_customer_id_containing(self, fragment: str) -> list[Order]: ...

    # IN a list
    async def find_by_status_in(self, statuses: list[str]) -> list[Order]: ...

    # IS NULL / IS NOT NULL (zero arguments consumed)
    async def find_by_deleted_at_is_null(self) -> list[Order]: ...
    async def find_by_email_is_not_null(self) -> list[User]: ...

    # COUNT prefix
    async def count_by_status(self, status: str) -> int: ...

    # EXISTS prefix
    async def exists_by_customer_id(self, customer_id: str) -> bool: ...

    # DELETE prefix (returns number of rows deleted)
    async def delete_by_status(self, status: str) -> int: ...

    # With ordering
    async def find_by_status_order_by_created_at_desc(
        self, status: str
    ) -> list[Order]: ...

    # Complex: AND + ordering
    async def find_by_status_and_customer_id_order_by_total_desc(
        self, status: str, customer_id: str
    ) -> list[Order]: ...
```

Each method body should be a stub (`...` or `pass`). The `RepositoryBeanPostProcessor` detects them and replaces them with real implementations at startup.

---

## Custom Queries with @query

For complex queries that cannot be expressed through method naming conventions, use the `@query` decorator:

```python
from pyfly.data.relational.sqlalchemy import query
```

### JPQL-Like Syntax

By default, `@query` accepts a JPQL-like query string that is transpiled to SQL at startup:

```python
@repo_stereotype
class OrderRepository(Repository[Order, UUID]):

    @query("SELECT o FROM Order o WHERE o.status = :status AND o.total > :min_total")
    async def find_expensive_orders(
        self, status: str, min_total: float
    ) -> list[Order]: ...

    @query("SELECT COUNT(o) FROM Order o WHERE o.role = :role")
    async def count_by_role(self, role: str) -> int: ...
```

Named parameters (`:param_name`) are bound from the method's keyword arguments.

### Native SQL

Set `native=True` for raw SQL queries:

```python
@query("SELECT * FROM orders WHERE status = :status", native=True)
async def find_by_status_native(self, status: str) -> list[Order]: ...
```

### Return Type Inference

The `QueryExecutor` infers the return type from the query shape:

| Query Pattern              | Return Type    |
|----------------------------|----------------|
| `SELECT COUNT(...)`        | `int`          |
| Query containing `EXISTS`  | `bool`         |
| All other `SELECT` queries | `list[entity]` |

### JPQL Transpilation Details

The lightweight JPQL-to-SQL transpiler performs these transformations:

1. `FROM Entity alias` becomes `FROM <tablename>` (alias is removed)
2. `SELECT alias` becomes `SELECT *`
3. `COUNT(alias)` becomes `COUNT(*)`
4. `alias.field` references become just `field` (alias prefix stripped)
5. Boolean literals `= true` / `= false` become `= 1` / `= 0`

Example transpilation:

```
JPQL:  SELECT u FROM User u WHERE u.email LIKE :pattern AND u.active = true
SQL:   SELECT * FROM users WHERE email LIKE :pattern AND active = 1
```

---

## Specifications

Specifications provide composable, type-safe query predicates inspired by Spring Data's Specification pattern. They let you build arbitrarily complex WHERE clauses from small, reusable building blocks.

> **Commons port:** The SQLAlchemy `Specification[T]` subclasses the generic `Specification[T, Q]` ABC from `pyfly.data.specification`. This means SQLAlchemy specifications are polymorphic with the commons port — code that accepts `pyfly.data.Specification` will work with the SQLAlchemy adapter. See the [Specification Port](data.md#specification-port) section in the Data Commons guide.

### Creating Specifications

A `Specification[T]` wraps a callable that takes an entity class (`root`) and a SQLAlchemy `Select` statement, and returns a modified `Select`:

```python
from pyfly.data.relational.sqlalchemy import Specification

# Inline specification
active = Specification(lambda root, q: q.where(root.active == True))
admin = Specification(lambda root, q: q.where(root.role == "admin"))
```

### Combining Specifications

Specifications support Python's standard operators for composition:

```python
# AND: both conditions must match
active_admins = active & admin

# OR: either condition may match
active_or_admin = active | admin

# NOT: negate a specification
inactive = ~active

# Complex combinations with parentheses
complex_spec = (active & admin) | ~admin
```

**How combination works internally:**

- `&` (AND): Chains the two predicates sequentially. SQLAlchemy naturally combines successive `.where()` calls with AND.
- `|` (OR): Applies each predicate independently, extracts the `whereclause` from each, and combines them using `sqlalchemy.or_()`.
- `~` (NOT): Applies the predicate, extracts the `whereclause`, and wraps it with `sqlalchemy.not_()`.

### Using Specifications with Repositories

```python
# Find all matching a specification
orders = await repo.find_all_by_spec(active & admin)

# Find with pagination
from pyfly.data import Pageable, Sort

pageable = Pageable.of(page=1, size=20, sort=Sort.by("created_at").descending())
page = await repo.find_all_by_spec_paged(active & admin, pageable)
```

---

## FilterOperator

`FilterOperator` provides a library of static factory methods for creating common `Specification` predicates without writing lambdas.

### Available Operators

| Method                            | SQL Equivalent                | Arguments      |
|-----------------------------------|-------------------------------|----------------|
| `eq(field, value)`                | `field = value`               | field, value   |
| `neq(field, value)`               | `field != value`              | field, value   |
| `gt(field, value)`                | `field > value`               | field, value   |
| `gte(field, value)`               | `field >= value`              | field, value   |
| `lt(field, value)`                | `field < value`               | field, value   |
| `lte(field, value)`               | `field <= value`              | field, value   |
| `like(field, pattern)`            | `field LIKE pattern`          | field, pattern |
| `contains(field, value)`          | `field LIKE '%value%'`        | field, value   |
| `in_list(field, values)`          | `field IN (values)`           | field, list    |
| `is_null(field)`                  | `field IS NULL`               | field          |
| `is_not_null(field)`              | `field IS NOT NULL`           | field          |
| `between(field, low, high)`       | `field BETWEEN low AND high`  | field, low, high|

### Composing Filters

Every `FilterOperator` method returns a `Specification`, so they can be composed with `&`, `|`, and `~`:

```python
from pyfly.data.relational.sqlalchemy import FilterOperator

# Adults between 18 and 65
age_filter = FilterOperator.gte("age", 18) & FilterOperator.lt("age", 65)

# Active users with a verified email
user_filter = FilterOperator.eq("active", True) & FilterOperator.is_not_null("email_verified_at")

# Premium or VIP customers
tier_filter = FilterOperator.in_list("tier", ["PREMIUM", "VIP"])

# Combine everything
final_spec = age_filter & user_filter & tier_filter
results = await repo.find_all_by_spec(final_spec)
```

---

## FilterUtils: Query by Example

> **Commons port:** `FilterUtils` extends the `BaseFilterUtils` ABC from `pyfly.data.filter`. The `by()`, `from_dict()`, and `from_example()` algorithms are inherited from the base class — `FilterUtils` only implements the adapter-specific hooks `_create_eq()` and `_create_noop()`. See the [BaseFilterUtils Port](data.md#basefilterutils-port) section in the Data Commons guide.

`FilterUtils` generates `Specification` objects from various input formats, providing a Pythonic take on Spring Data's Query by Example pattern.

```python
from pyfly.data.relational.sqlalchemy import FilterUtils

# From keyword arguments (all eq, ANDed together)
spec = FilterUtils.by(name="Alice", active=True)
results = await repo.find_all_by_spec(spec)

# From a dictionary (None values are automatically skipped)
filters = {"role": "admin", "name": None, "active": True}
spec = FilterUtils.from_dict(filters)
# Produces: role = 'admin' AND active = True (name is skipped)

# From an example object (dataclass or plain object)
# Non-None fields become equality predicates
from dataclasses import dataclass

@dataclass
class UserFilter:
    role: str | None = None
    active: bool | None = None

example = UserFilter(role="admin")
spec = FilterUtils.from_example(example)
# Produces: role = 'admin' (active is None, so skipped)
```

**FilterUtils methods:**

| Method                    | Input                | Behavior                                    |
|---------------------------|----------------------|---------------------------------------------|
| `by(**kwargs)`            | Keyword arguments    | All eq, ANDed together                      |
| `from_dict(filters)`      | `dict[str, Any]`     | All eq, ANDed; `None` values skipped        |
| `from_example(example)`   | Dataclass or object  | Non-`None` fields become eq predicates      |

---

## Pagination

For the full `Pageable`, `Sort`, `Order`, and `Page[T]` API reference, see the [Data Module Guide — Pagination & Sorting](data.md#pagination--sorting).

### Paginated Queries

```python
from pyfly.data import Pageable, Sort

# Basic pagination
page = await repo.find_paginated(page=1, size=20)

# With Pageable (overrides page/size and adds sorting)
pageable = Pageable.of(page=2, size=10, sort=Sort.by("name"))
page = await repo.find_paginated(pageable=pageable)
```

When `pageable` is provided, its `page`, `size`, and `sort` override the primitive `page` and `size` arguments.

### Paginated Specification Queries

```python
spec = FilterOperator.eq("status", "ACTIVE")
pageable = Pageable.of(page=1, size=20, sort=Sort.by("created_at").descending())

page = await repo.find_all_by_spec_paged(spec, pageable)
# Returns Page[Order] with filtered, sorted, paginated results
```

The implementation:
1. Applies the specification's predicate to get the filtered query.
2. Counts total matching rows via a subquery.
3. Applies sort orders from `Pageable.sort`.
4. Applies `offset` and `limit` for pagination.

---

## Transaction Management

The `@reactive_transactional` decorator provides declarative async transaction management:

```python
from pyfly.data.relational.sqlalchemy import reactive_transactional
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

session_factory: async_sessionmaker[AsyncSession] = ...


@reactive_transactional(session_factory)
async def create_order(session: AsyncSession, customer_id: str) -> Order:
    order = Order(customer_id=customer_id, status="PENDING")
    session.add(order)
    return order
    # Transaction is automatically committed on success
    # Transaction is automatically rolled back on exception
```

**How it works:**
1. Opens a new `AsyncSession` from the `session_factory`.
2. Begins a transaction with `session.begin()`.
3. Calls the wrapped function with the session as the first argument.
4. On success: the transaction is committed (via the `async with` context manager).
5. On exception: the transaction is rolled back and the exception is re-raised.

The decorated function's original arguments are passed after the injected `session`:

```python
@reactive_transactional(session_factory)
async def transfer_funds(session: AsyncSession, from_id: str, to_id: str, amount: float):
    # session is injected; from_id, to_id, amount are passed through
    ...

# Call it without the session argument:
await transfer_funds("acc-1", "acc-2", 100.0)
```

---

## Read/Write Routing (Read Replicas)

PyFly can route read-only work to a database **read replica** while keeping writes on the primary — the equivalent of Spring's `AbstractRoutingDataSource` driven by `@Transactional(readOnly = true)`. Routing is **opt-in**: with no replica configured, every session goes to the primary, so behavior is unchanged for existing apps.

```python
from pyfly.data.relational.routing import RoutingSessionFactory, read_only, is_read_only
```

### Enabling a Replica

Set the replica URL under `pyfly.data.relational.read-replica.url`. `RelationalAutoConfiguration` then builds a separate engine + `async_sessionmaker` for the replica and wires it into the `routing_session_factory` bean:

```yaml
pyfly:
  data:
    relational:
      url: postgresql+asyncpg://user:pass@primary:5432/app
      read-replica:
        url: postgresql+asyncpg://user:pass@replica:5432/app
```

When `read-replica.url` is absent, `routing_session_factory` is still registered but has no replica — it always returns a primary session.

### RoutingSessionFactory

`RoutingSessionFactory` is a drop-in replacement for an `async_sessionmaker` call site: calling the factory (`factory()`) returns an `AsyncSession`, routed by context.

| Member | Returns | Description |
|--------|---------|-------------|
| `factory()` (`__call__`) | `AsyncSession` | Routes by context: the replica when inside a `read_only()` block **and** a replica is configured; otherwise the primary. |
| `factory.primary()` | `AsyncSession` | Forces a primary (read/write) session regardless of context. |
| `factory.replica()` | `AsyncSession` | Forces a replica session; falls back to the primary when none is configured. |
| `factory.has_replica` | `bool` | Whether a replica session maker is configured. |

### read_only() and is_read_only()

The `read_only()` context manager marks the enclosed block read-only so the factory routes to the replica (the `@Transactional(readOnly = true)` analogue). It is backed by a `ContextVar`, so it is safe across `async`/await and supports nesting — the prior value is restored on exit. `is_read_only()` reports whether the current context is marked read-only.

```python
from pyfly.container import service
from pyfly.data.relational.routing import RoutingSessionFactory, read_only
from sqlalchemy import select


@service
class UserService:
    def __init__(self, sessions: RoutingSessionFactory) -> None:
        self._sessions = sessions

    async def list_users(self) -> list[User]:
        with read_only():                       # routes to the replica when one is configured
            session = self._sessions()          # AsyncSession bound to the replica
            result = await session.execute(select(User))
            return list(result.scalars())

    async def create_user(self, name: str) -> User:
        session = self._sessions()              # no read_only() -> primary (read/write)
        user = User(name=name)
        session.add(user)
        await session.commit()
        return user
```

Outside any `read_only()` block, `factory()` always returns a primary session. Inside one, it returns a replica session **only if** a replica is configured; otherwise it falls back to the primary, so the same code runs unchanged in environments without a replica.

**Source:** `src/pyfly/data/relational/routing.py` · bean: `RelationalAutoConfiguration.routing_session_factory`

---

## Data Auditing

PyFly provides automatic entity auditing through the `AuditingEntityListener`. It auto-populates the `created_at`, `updated_at`, `created_by`, and `updated_by` fields on `BaseEntity` subclasses via SQLAlchemy ORM events, so you never need to set these fields manually.

### AuditingEntityListener

```python
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener
```

The `AuditingEntityListener` registers SQLAlchemy `before_insert` and `before_update` event listeners on `BaseEntity`. Because the listeners use `propagate=True`, they automatically apply to all subclasses of `BaseEntity`.

### How Auditing Works

| Event | Fields Set | Behavior |
|---|---|---|
| `before_insert` | `created_at`, `updated_at`, `created_by`, `updated_by` | Sets both timestamps to `datetime.now(UTC)`. Sets both user fields to the current authenticated user (if available). |
| `before_update` | `updated_at`, `updated_by` | Sets `updated_at` to `datetime.now(UTC)`. Sets `updated_by` to the current authenticated user (if available). |

**Example:**

```python
from pyfly.data.relational.sqlalchemy import BaseEntity
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class Order(BaseEntity):
    __tablename__ = "orders"
    customer_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))

# When you save a new Order, the audit fields are populated automatically:
order = Order(customer_id="abc", status="PENDING")
saved = await repo.save(order)
# saved.created_at = 2026-02-20T10:30:00+00:00
# saved.updated_at = 2026-02-20T10:30:00+00:00
# saved.created_by = "user-123"  (from SecurityContext)
# saved.updated_by = "user-123"

# On subsequent updates, only updated_at and updated_by change:
saved.status = "SHIPPED"
updated = await repo.save(saved)
# updated.created_at = 2026-02-20T10:30:00+00:00  (unchanged)
# updated.updated_at = 2026-02-20T10:35:00+00:00  (new timestamp)
# updated.created_by = "user-123"                  (unchanged)
# updated.updated_by = "admin-456"                 (new user)
```

### Resolving the Current User

The `AuditingEntityListener` resolves the current authenticated user from the `RequestContext`:

1. Calls `RequestContext.current()` to get the current request context.
2. If a `RequestContext` is available, reads the `security_context` attribute.
3. If the `SecurityContext` is authenticated (`is_authenticated` is `True`), uses `user_id` as the value for `created_by` / `updated_by`.
4. If there is no `RequestContext` or the user is not authenticated, the user fields are left unchanged (they remain `None` for new entities).

This means auditing works transparently in HTTP request handlers (where the `SecurityMiddleware` or `SecurityFilter` populates the `SecurityContext`) and degrades gracefully in background tasks or CLI scripts where no request context is available.

### Registering the Listener

The `AuditingEntityListener` must be registered once at application startup. Call `register()` to attach the ORM event listeners:

```python
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener

listener = AuditingEntityListener()
listener.register()
```

When using auto-configuration, the listener is registered automatically by `RelationalAutoConfiguration` when `pyfly.data.relational.enabled` is `true`. No manual registration is needed in that case.

You can also register it in a custom `@configuration` class:

```python
from pyfly.container import configuration, bean
from pyfly.data.relational.sqlalchemy.auditing import AuditingEntityListener


@configuration
class DataConfig:

    @bean
    def auditing_listener(self) -> AuditingEntityListener:
        listener = AuditingEntityListener()
        listener.register()
        return listener
```

**Source:** `src/pyfly/data/relational/sqlalchemy/auditing.py`

---

### Transaction Management

The `@transactional` decorator provides Spring-style declarative transaction management with propagation and isolation support.

#### Basic Usage

```python
from pyfly.data.relational.sqlalchemy import transactional, Propagation, Isolation

@service
class OrderService:
    _session_factory: async_sessionmaker  # injected by DI

    @transactional()
    async def create_order(self, order: Order) -> Order:
        return await self.repo.save(order)

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def audit_log(self, message: str) -> None:
        # Always opens a new transaction, even if called from within another
        ...

    @transactional(isolation=Isolation.SERIALIZABLE, read_only=True)
    async def generate_report(self) -> Report:
        ...
```

#### Propagation Types

| Propagation | Behavior |
|------------|----------|
| `REQUIRED` (default) | Join existing transaction or start new |
| `REQUIRES_NEW` | Always start a new transaction, suspending existing |
| `SUPPORTS` | Run within transaction if one exists, or without |
| `NOT_SUPPORTED` | Suspend any existing transaction |
| `MANDATORY` | Require existing transaction, raise if none |
| `NEVER` | Raise if a transaction exists |

#### Isolation Levels

`DEFAULT`, `READ_UNCOMMITTED`, `READ_COMMITTED`, `REPEATABLE_READ`, `SERIALIZABLE`

The decorator resolves `async_sessionmaker` from `self._session_factory` and automatically patches Repository instances on the service with the transaction-scoped session.

---

### Soft Delete & Optimistic Locking

PyFly provides opt-in mixins for soft delete and optimistic locking, mirroring JPA's `@SoftDelete` and `@Version` annotations.

#### SoftDeleteMixin

```python
from pyfly.data.relational.sqlalchemy import BaseEntity, SoftDeleteMixin

class Order(BaseEntity, SoftDeleteMixin):
    __tablename__ = "orders"
    name: Mapped[str] = mapped_column(String(255))
```

This adds a `deleted_at` column. Use `SoftDeleteRepository` for automatic soft-delete-aware CRUD:

```python
from pyfly.data.relational.sqlalchemy import SoftDeleteRepository

class OrderRepository(SoftDeleteRepository[Order, UUID]):
    pass  # delete() sets deleted_at, find methods exclude deleted entities
```

| Method | Behavior |
|--------|----------|
| `delete(id)` | Sets `deleted_at` (soft delete) |
| `find_by_id(id)` | Excludes soft-deleted entities |
| `find_all()` | Excludes soft-deleted entities |
| `find_paginated(page, size, pageable)` | Excludes soft-deleted entities; respects pagination and sort |
| `find_all_by_ids(ids)` | Excludes soft-deleted entities |
| `find_all_by_spec(spec)` | Applies spec predicate AND excludes soft-deleted entities |
| `find_all_by_spec_paged(spec, pageable)` | Applies spec predicate AND excludes soft-deleted entities |
| `find_all_including_deleted()` | Includes soft-deleted entities |
| `restore(id)` | Clears `deleted_at` |
| `hard_delete(id)` | Permanently removes from DB |
| `count()` | Counts only non-deleted entities |

#### VersionedMixin (Optimistic Locking)

```python
from pyfly.data.relational.sqlalchemy import BaseEntity, VersionedMixin

class Order(BaseEntity, VersionedMixin):
    __tablename__ = "orders"
    name: Mapped[str] = mapped_column(String(255))
```

This adds a `version` column. SQLAlchemy automatically appends `WHERE version = :old` to every UPDATE and raises `StaleDataError` on concurrent modification — the equivalent of JPA's `@Version`.

---

## RepositoryBeanPostProcessor

The `RepositoryBeanPostProcessor` is a `BeanPostProcessor` that runs after each repository bean is initialized. It scans the repository class for stub methods and replaces them with real query implementations.

### How It Works

The `after_init(bean, bean_name)` method:

1. Checks if the bean is an instance of `Repository`. If not, it is returned unchanged.
2. Gets the entity type from `bean._model`.
3. Iterates over all attributes defined on the bean's class (not inherited from `Repository`).
4. For `@query`-decorated methods: compiles them via `QueryExecutor.compile_query_method()` and replaces the stub with a wrapper that injects `bean._session`.
5. For derived query methods (`find_by_*`, `count_by_*`, `exists_by_*`, `delete_by_*`): checks if the method is a stub, parses the method name via `QueryMethodParser.parse()`, compiles it via `QueryMethodCompiler.compile()`, and replaces the stub with a wrapper.

### Stub Detection

A method is considered a stub when its code object contains no meaningful constants beyond `None` and `Ellipsis`. This covers both forms:

```python
async def find_by_status(self, status: str) -> list[Order]: ...    # Ellipsis stub
async def find_by_status(self, status: str) -> list[Order]: pass   # Pass stub
```

Register the post-processor in your application context:

```python
from pyfly.data.relational.sqlalchemy import RepositoryBeanPostProcessor

context.register_post_processor(RepositoryBeanPostProcessor())
```

---

## QueryMethodCompiler

The SQLAlchemy `QueryMethodCompiler` implements the [`QueryMethodCompilerPort`](data.md#querymethodcompilerport) protocol. It takes `ParsedQuery` objects produced by the shared `QueryMethodParser` and compiles them into SQLAlchemy column expressions.

| Prefix       | Generated Query Pattern                             |
|--------------|-----------------------------------------------------|
| `find_by`    | `SELECT entity WHERE ... ORDER BY ...`              |
| `count_by`   | `SELECT COUNT(*) FROM entity WHERE ...`             |
| `exists_by`  | `SELECT COUNT(*) FROM entity WHERE ... > 0`         |
| `delete_by`  | `DELETE FROM entity WHERE ...` (returns `rowcount`) |

The compiler builds SQLAlchemy column expressions from each `FieldPredicate`, combines them using the connectors, and applies ORDER BY clauses from `OrderClause` objects.

---

## Complete CRUD Example

The following example demonstrates an entity, repository with derived queries, specifications, pagination, and mapping.

```python
# --- Entity ---

from pyfly.data.relational.sqlalchemy import BaseEntity
from sqlalchemy import String, Float, Boolean
from sqlalchemy.orm import Mapped, mapped_column


class Product(BaseEntity):
    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(255))
    price: Mapped[float] = mapped_column(Float)
    category: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# --- Repository ---

from pyfly.data.relational.sqlalchemy import Repository, query
from pyfly.container import repository as repo_stereotype
from sqlalchemy.ext.asyncio import AsyncSession


@repo_stereotype
class ProductRepository(Repository[Product, UUID]):

    # Derived query methods (stubs, auto-compiled at startup)
    async def find_by_category(self, category: str) -> list[Product]: ...

    async def find_by_active_and_category(
        self, active: bool, category: str
    ) -> list[Product]: ...

    async def find_by_price_greater_than_order_by_price_desc(
        self, min_price: float
    ) -> list[Product]: ...

    async def find_by_name_containing(self, fragment: str) -> list[Product]: ...

    async def count_by_category(self, category: str) -> int: ...

    async def exists_by_name(self, name: str) -> bool: ...

    async def delete_by_active(self, active: bool) -> int: ...

    # Custom query method
    @query("SELECT p FROM Product p WHERE p.category = :category AND p.price > :min_price")
    async def find_expensive_in_category(
        self, category: str, min_price: float
    ) -> list[Product]: ...


# --- Service ---

from pyfly.container import service
from pyfly.data import Pageable, Sort, Page, Mapper
from pyfly.data.relational.sqlalchemy import FilterOperator, FilterUtils, Specification
from dataclasses import dataclass


@dataclass
class ProductDTO:
    id: str
    name: str
    price: float
    category: str


@service
class ProductService:
    def __init__(self, repo: ProductRepository) -> None:
        self._repo = repo
        self._mapper = Mapper()

    async def find_all_active(self, category: str | None = None) -> list[ProductDTO]:
        spec: Specification = FilterOperator.eq("active", True)
        if category:
            spec = spec & FilterOperator.eq("category", category)
        products = await self._repo.find_all_by_spec(spec)
        return self._mapper.map_list(products, ProductDTO)

    async def find_paginated(
        self,
        page: int = 1,
        size: int = 20,
        category: str | None = None,
    ) -> Page[ProductDTO]:
        spec = FilterOperator.eq("active", True)
        if category:
            spec = spec & FilterOperator.eq("category", category)

        pageable = Pageable.of(
            page=page,
            size=size,
            sort=Sort.by("name"),
        )

        result = await self._repo.find_all_by_spec_paged(spec, pageable)
        return result.map(lambda p: self._mapper.map(p, ProductDTO))

    async def search_by_name(self, query: str) -> list[ProductDTO]:
        products = await self._repo.find_by_name_containing(query)
        return self._mapper.map_list(products, ProductDTO)

    async def find_by_id(self, product_id: str) -> ProductDTO | None:
        from uuid import UUID
        product = await self._repo.find_by_id(UUID(product_id))
        if product is None:
            return None
        return self._mapper.map(product, ProductDTO)

    async def create(self, name: str, price: float, category: str) -> ProductDTO:
        product = Product(name=name, price=price, category=category)
        saved = await self._repo.save(product)
        return self._mapper.map(saved, ProductDTO)

    async def delete(self, product_id: str) -> None:
        from uuid import UUID
        await self._repo.delete(UUID(product_id))

    async def count_in_category(self, category: str) -> int:
        return await self._repo.count_by_category(category)
```

---

## See Also

- [Data Module Guide](data.md) — Generic commons: repository ports, pagination, query parsing, entity mapping, extensibility
- [Data Document Guide](data-document.md) — MongoDB adapter
- [SQLAlchemy Adapter Reference](../adapters/sqlalchemy.md) — Setup, configuration, adapter-specific features
