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
  - [Unit of Work](#unit-of-work)
  - [Programmatic Transactions](#programmatic-transactions)
  - [reactive_transactional](#reactive_transactional)
- [Run Migrations on Startup (Flyway-Style)](#run-migrations-on-startup-flyway-style)
- [Datasource Registry](#datasource-registry)
  - [Configuration Reference](#configuration-reference)
  - [SQLite Setup](#sqlite-setup)
  - [Module Datasources](#module-datasources)
  - [Framework Tables](#framework-tables)
  - [After-Begin Customizers](#after-begin-customizers)
  - [Credential Rotation](#credential-rotation)
  - [Capabilities](#capabilities)
- [Read/Write Routing (Read Replicas)](#readwrite-routing-read-replicas)
- [Multiple Named Datasources](#multiple-named-datasources)
  - [NamedDataSources](#nameddatasources)
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
    transactional, reactive_transactional,  # Declarative transaction management
)
from pyfly.data.transaction import (     # The backend-neutral unit of work
    TransactionTemplate, after_commit, detached, infrastructure_unit,
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

When you subclass `Repository[T, ID]` with concrete type parameters, the framework automatically extracts the entity type and ID type via `__init_subclass__`. A repository holds no session: it resolves one on every call through the [unit of work](#unit-of-work). No explicit `__init__` is needed:

```python
from uuid import UUID
from pyfly.data.relational.sqlalchemy import Repository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class OrderRepository(Repository[Order, UUID]):
    pass

# Outside a transaction, each call runs in a short unit of its own and commits:
order = await repo.save(Order(customer_id="abc", status="PENDING"))
found = await repo.find_by_id(order.id)
```

`Repository[T, ID]` satisfies the [`RepositoryPort[T, ID]`](data.md#repository-ports) protocol, enabling hexagonal architecture where your service layer depends on the port, not the adapter. The port hierarchy is `CrudRepository[T, ID]` -> `ReactiveSortingRepository[T, ID]` -> `PagingAndSortingRepository[T, ID]` (mirroring Spring Data WebFlux's `ReactiveCrudRepository` -> `ReactiveSortingRepository` + paging), and `RepositoryPort` is an alias of `CrudRepository`.

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

1. `__init_subclass__` inspects `__orig_bases__` to extract the entity type (`Order`) and ID type (`UUID`) from the generic parameters at class definition time. It works for a subclass of `SoftDeleteRepository[T, ID]` too.
2. The container never injects a session into a repository: the `session` parameter is `Annotated[AsyncSession | None, NoAutowire]`. A DI-built repository is therefore in **managed mode** and resolves its session per call (see [Unit of Work](#unit-of-work)). `Repository(Order, session)` with an explicit session is **manual mode**: the caller owns that session, and the repository uses it as is.
3. The entity type is used internally for all query operations — no need to pass it manually.
4. `__datasource__ = "reporting"` on the class (or `Repository(Order, datasource="reporting")`) gives the repository its datasource; the default is the primary.

Custom methods keep working: `self._session` and `self._require_session()` return the session of the current call. Every public `async def` of a subclass is wrapped like the inherited methods, so a custom method is one operation: inside a unit it joins, outside one it runs in an auto unit (a read unit when its name starts with `find`, `count`, `exists`, `stream` or `get`; a write unit that commits otherwise).

### CRUD Methods Reference

| Method                                           | Return Type         | Description                                              |
|--------------------------------------------------|---------------------|---------------------------------------------------------|
| `save(entity)`                                   | `T`                 | Insert or update; flushes and refreshes                 |
| `find_by_id(id: ID)`                             | `T \| None`         | Find by primary key                                     |
| `find_all(**filters)`                             | `list[T]`           | Find all, optionally filtered by column values          |
| `find_all(sort: Sort)`                            | `list[T]`           | Fetch all, applying the `Sort` order                    |
| `find_all(pageable: Pageable)`                    | `Page[T]`           | Paginated query: counts total, applies sort, slices     |
| `find_all_by_id(ids)`                            | `list[T]`           | Find all entities whose IDs are in `ids`                |
| `stream_all(criteria: Sort \| None, **filters)`  | `AsyncIterator[T]`  | Stream all (the `Flux[T]` analogue); optional `Sort`    |
| `delete(entity: T)`                              | `None`              | Delete the given entity                                 |
| `delete_by_id(id: ID)`                           | `None`              | Delete by primary key (no-op if not found)              |
| `delete_all(entities=None)`                      | `None`              | Delete the given entities; with no args, truncate all   |
| `delete_all_by_id(ids)`                          | `None`              | Delete all entities whose IDs are in `ids`              |
| `count()`                                         | `int`               | Count all entities in the table                         |
| `exists_by_id(id: ID)`                           | `bool`              | Check if an entity with this ID exists                  |
| `find_all_by_spec(spec)`                          | `list[T]`           | Find all matching a Specification                       |
| `find_all_by_spec_paged(spec, pageable)`          | `Page[T]`           | Paginated query with Specification + sorting            |

**save()** calls `session.add()`, then `session.flush()` and `session.refresh()` to ensure the returned entity has all database-generated values (ID, defaults, etc.).

**find_all()** accepts keyword arguments that are translated into equality filters:

```python
orders = await repo.find_all(status="PENDING", customer_id="abc")
# Equivalent to: SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 'abc'
```

**delete_by_id()** looks up the entity first and deletes it if found. If not found, it is a no-op. **delete(entity)** removes the given entity directly. The `delete_all(entities)` form deletes each given entity, while `delete_all()` with no arguments truncates the whole table; both return `None`. `delete_all_by_id(ids)` deletes every entity whose ID is in `ids`.

**find_all(pageable)** counts the total, applies the `Pageable`'s sort, slices with `LIMIT`/`OFFSET`, and returns a `Page[T]`. **find_all(sort)** fetches every row in the given `Sort` order, and **stream_all(criteria=Sort.by(...))** yields entities one at a time as an `AsyncIterator[T]` (the `Flux[T]` analogue):

```python
from pyfly.data import Sort

async for order in repo.stream_all(Sort.by("name")):
    process(order)
```

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

For the full `Pageable`, `Sort`, `Order`, and `Page[T]` API reference, see the [Data Module Guide — Pagination & Sorting](data.md#pagination-sorting).

### Paginated Queries

```python
from pyfly.data import Pageable, Sort

# Basic pagination
page = await repo.find_all(Pageable.of(page=1, size=20))

# With Pageable (page, size and sorting)
pageable = Pageable.of(page=2, size=10, sort=Sort.by("name"))
page = await repo.find_all(pageable)
```

`find_all(pageable)` counts the total, applies the `Pageable`'s sort, slices with `LIMIT`/`OFFSET`, and returns a `Page[T]`. `Pageable` is 1-based, so `page=1` is the first page.

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

PyFly binds every transaction to the running task as a **unit of work** and never mutates a bean: two
concurrent requests through the same singleton service each get their own unit, and a repository call
reaches the unit wherever the repository lives (a nested service, a list, a `Provider`, `get_bean`). The
backend-neutral package is `pyfly.data.transaction`; `@transactional` (below, in
[Transaction Management with @transactional](#transaction-management-with-transactional)) is its
declarative face, and one `SqlAlchemyTransactionManager` per datasource of the
[registry](#datasource-registry) runs the units.

### Unit of Work

A repository call resolves its session when it runs:

- **Inside a unit for its datasource** (`@transactional`, a `TransactionTemplate` block, a
  `reactive_transactional` function), it joins that unit and uses its session.
- **Outside a transaction**, the outermost repository call opens a short **auto unit** of its own, and
  nested repository calls inside it share it (a custom method calling `count()` and `exists_by_id()` is
  one unit):
  - a **read** method (`find*`, `count*`, `exists*`, `stream*`, `get*`) gets a read unit. On
    PostgreSQL it runs on an `AUTOCOMMIT` connection, one round trip instead of three, unless the
    datasource has [after-begin customizers](#after-begin-customizers) (their transaction-local settings
    need a transaction). Elsewhere it is a short transaction that ends without writing. A read unit whose
    connection turns out to be dead (a failover, a server-side idle timeout) is retried once on a fresh
    connection, so pool pre-ping is not needed. An ORM write, or a Core `insert()`/`update()`/`delete()`,
    inside a read unit is refused before it reaches the database. A raw `text()` statement is not
    inspected: on PostgreSQL a read unit's `AUTOCOMMIT` connection would commit it at once, and on SQLite
    its unit would roll it back, so give a method that writes a name that is not a read name. The rule
    is the prefix alone: `get_or_create`, `find_or_create` and `find_and_update` are read methods too,
    and their writes are refused outside a transaction. Call such a method inside `@transactional`, or
    name it `create_if_missing`, `upsert` or similar.
  - any **other** method gets a write unit that commits (on SQLite, it starts with `BEGIN IMMEDIATE`).

  Either way the connection goes back to the pool when the call returns. Entities returned from an auto
  unit are detached with their loaded state intact (`expire_on_commit=False`); lazy relationships need an
  explicit fetch.
- `stream_all` captures the unit at its first step, or opens its own read unit (always a transaction:
  server-side cursors need one), and owns that connection until the iterator is exhausted or
  `aclose()`d. Close an abandoned stream with `contextlib.aclosing(...)`: closing it early closes its
  cursor at once (on MySQL and MariaDB that reads the rest of its rows and drops them, as the
  connection requires). A stream left open (a `break` out of the loop does not close it) is closed
  before its unit's `COMMIT` or `ROLLBACK`, and before the `NESTED` step or savepoint block it was
  opened in ends.

Every `asyncio` task created inside a transaction inherits its unit. That is made safe:

- Operations on a unit's session run under the unit's **operation guard**, a lock that is reentrant per
  task. `asyncio.gather()` fan-out inside `@transactional` is serialized, and the framework's repository
  methods are atomic (`save` is add, flush and refresh as one step). An atomic method never calls a
  method a subclass may override while it holds the guard (`exists_by_id` does not go through
  `find_by_id`), so an override that fans out cannot wait for its own caller.
- Savepoints do not fan out. They are a stack on the unit's one connection, and the guard does not span
  the code inside a savepoint, so while a task holds one (a `Propagation.NESTED` step, a
  `session.begin_nested()` block) the unit belongs to that task and to the tasks it starts inside the
  savepoint. Any operation from another task, such as a sibling in `gather()`, raises
  `IllegalTransactionStateError` instead of running inside that savepoint, where a `ROLLBACK TO
  SAVEPOINT` would undo it after it reported success: a write, a read (it would see work the savepoint
  may still undo, and its autoflush would write inside it), a fetch from a stream, a savepoint or a
  `NESTED` step. Run `NESTED` steps and savepoint blocks one after another; to run steps concurrently,
  give each one a unit of its own (`Propagation.REQUIRES_NEW`, which commits on its own, or
  `detached()`; on SQLite a child task's write unit fails with `database is locked` while its parent's
  write unit is open, see [SQLite Setup](#sqlite-setup)).

  The check sees operations, not attribute changes. The unit has one session, so an entity one task
  loaded is the entity every task of the unit gets, and a change a sibling makes to it
  (`order.status = "paid"`, with no statement of its own) stays pending until the next flush. When that
  flush is the autoflush of a query the savepoint's task runs, the change is written inside that
  savepoint and rolled back with it, after the sibling reported success. Do not change loaded entities
  from concurrent tasks inside a unit: change them in the task that holds the savepoint, or after the
  `NESTED` steps end.

  A `NESTED` scope that ends while a task it started still holds a savepoint on top of its own neither
  releases nor rolls back across it: the unit is marked rollback-only and the scope raises
  `IllegalTransactionStateError`. A unit does not commit under such a task either: a `@transactional`
  method (or a repository call's own unit) that returns while a child task it started still holds a
  savepoint rolls back and raises `IllegalTransactionStateError`, since committing would release that
  savepoint under the child and keep its work even if the child then fails.

  ```python
  @transactional
  async def import_rows(self, rows: list[Row]) -> list[Row]:
      rejected = []
      for row in rows:  # one after another: not asyncio.gather()
          try:
              await self.importer.import_row(row)  # @transactional(propagation=Propagation.NESTED)
          except IntegrityError:
              rejected.append(row)  # rolled back to its savepoint; the rest of the unit goes on
      return rejected
  ```
- On MySQL and MariaDB a connection has one active result at a time, so an open stream (`stream_all`,
  `session.stream()`) holds its unit until it is exhausted or closed: any other statement on the unit
  meanwhile, from the stream's own loop or from a sibling in `gather()`, raises
  `IllegalTransactionStateError` naming the stream, before it reaches the server (asyncmy would corrupt
  the connection instead). The stream goes on, and the unit stays usable. Collect the rows first, close
  the stream early, or give the other work a unit of its own (`Propagation.REQUIRES_NEW`, `detached()`).
  PostgreSQL and SQLite run other statements beside an open stream
  (`DataSource.capabilities.multiple_active_results` tells which kind a datasource is).

  ```python
  @transactional
  async def reprice(self) -> None:
      stale = [product async for product in self.products.stream_all() if product.stale]
      for product in stale:  # after the stream: on MySQL a save inside the loop above is refused
          await self.products.save(product.repriced())
  ```
- A task that uses a unit after it completed gets `IllegalTransactionStateError` naming the unit,
  instead of writing into a transaction nobody will commit. So does a `NESTED` step whose unit ended
  under it, whether its body returns or raises: its savepoint can no longer be released or rolled back,
  and its work went with the unit.
- Work that must outlive its caller's unit runs through `detached()`, with the transaction state cleared:

```python
from pyfly.data.transaction import detached

@transactional
async def place(self, order: Order) -> None:
    await self.orders.save(order)
    detached(self.notifier.send_receipt(order.id))   # its own transactions, outside place()'s unit


@detached                                            # every call schedules a task with its own state
async def rebuild_projection(order_id: str) -> None: ...
```

A statement that fails inside a unit marks it **rollback-only**: on PostgreSQL the transaction is dead
after any failed statement, and the same rule on every backend gives the same code the same outcome (a
failure inside a savepoint the application rolls back does not count; see
[Rollback rules and rollback-only](#rollback-rules-and-rollback-only)). A
unit whose task is cancelled (a client disconnect, a timeout) discards its connection instead of
returning it to the pool, and commit, rollback and release run shielded (their own task, under `asyncio`
and `anyio` shields) before the cancellation is re-raised.

### Programmatic Transactions

`TransactionTemplate` has the same semantics as `@transactional`:

```python
from pyfly.data.transaction import Propagation, TransactionTemplate

template = TransactionTemplate("reporting", timeout=5)          # a datasource name, a manager or None

async with template.transaction() as unit:                      # unit.resource is the AsyncSession
    await ledger.save(entry)

total = await template.execute(ledger.recompute)                # a coroutine function inside a unit

async with template.transaction(propagation=Propagation.REQUIRES_NEW):
    ...
```

The datasource is named once, in any of three places: the first argument (`TransactionTemplate("reporting")`),
the `datasource=` setting (`TransactionTemplate(datasource="reporting")`), or a per-call override
(`template.transaction(datasource="reporting")`). With none, the template runs on the default datasource.
A manager argument and a `datasource=` that name different datasources raise
`IllegalTransactionStateError` instead of one of them winning.

For custom data access code, inject `SessionProvider`: `current()` is the session of the current unit
(or `None`), and `async with provider.unit(read_only=...)` joins the bound unit or opens a short one.
Framework adapters use `infrastructure_unit(datasource)` from `pyfly.data.transaction`, the same
join-or-own helper: inside a business transaction their writes are part of it, outside one they get a
unit that commits. `single_statement=True` runs one statement on an `AUTOCOMMIT` connection where the
backend makes that cheaper (PostgreSQL).

```python
from pyfly.data.relational.sqlalchemy.session import SessionProvider

@service
class ReportDao:
    def __init__(self, sessions: SessionProvider) -> None:
        self._sessions = sessions

    async def totals(self) -> list[Row]:
        async with self._sessions.unit(read_only=True) as session:
            return (await session.execute(text("SELECT ..."))).all()
```

The transient `async_session` bean is a `ScopedAsyncSession`: each injection is a distinct object, and
inside a unit for its datasource its unit-of-work API (`execute`, `scalar`, `scalars`, `get`, `get_one`,
`add`, `add_all`, `delete`, `merge`, `flush`, `refresh`, `stream`, `stream_scalars`, `begin_nested`,
`in_transaction`) delegates to the unit's session, so a DAO that injects `AsyncSession` joins
`@transactional`. There its `commit()` and `rollback()` raise `IllegalTransactionStateError`, as Spring's
shared `EntityManager` does. Outside a unit it is an ordinary session its owner commits and closes.

### reactive_transactional

`@reactive_transactional(session_factory)` passes the unit's `AsyncSession` as the first argument:

```python
from pyfly.data.relational.sqlalchemy import reactive_transactional
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

session_factory: async_sessionmaker[AsyncSession] = ...


@reactive_transactional(session_factory)
async def create_order(session: AsyncSession, customer_id: str) -> Order:
    order = Order(customer_id=customer_id, status="PENDING")
    session.add(order)
    return order
    # Committed on success, rolled back on an exception
```

It is a `REQUIRED` boundary built on the `TransactionTemplate`, so it binds its unit: inside a unit on
the same datasource it joins (and receives that unit's session), `@transactional` methods called inside
it join it, and `MANDATORY`/`NEVER` see it. The decorated function's own arguments follow the session:

```python
@reactive_transactional(session_factory)
async def transfer_funds(session: AsyncSession, from_id: str, to_id: str, amount: float):
    ...

await transfer_funds("acc-1", "acc-2", 100.0)
```

---

## Run Migrations on Startup (Flyway-Style)

By default schema migrations are applied with the [`pyfly db`](../cli.md#pyfly-db) CLI commands. PyFly can also apply them **automatically on application startup** — the equivalent of Spring Boot's Flyway/Liquibase auto-migrate. This is **opt-in** and reuses the existing Alembic environment created by `pyfly db init`; the CLI commands keep working exactly as before.

Enable it with `pyfly.data.relational.migrations.enabled`:

```yaml
pyfly:
  data:
    relational:
      url: postgresql+asyncpg://user:pass@primary:5432/app
      migrations:
        enabled: true            # apply `alembic upgrade head` on startup
        config: alembic.ini      # path to the Alembic config (default: alembic.ini)
        revision: head           # target revision (default: head)
```

When enabled, `MigrationAutoConfiguration` registers a `MigrationRunner` bean. `MigrationRunner` implements the `start()` / `stop()` lifecycle, so the `ApplicationContext` auto-discovers it as an infrastructure adapter and calls `start()` once during startup. On `start()` it runs `alembic upgrade <revision>` against the **same datasource** the app uses (it forwards `pyfly.data.relational.url` into Alembic's `sqlalchemy.url`, so there is a single source of truth for the connection string).

The upgrade runs in a worker thread (`asyncio.to_thread`) because the generated async `alembic/env.py` calls `asyncio.run` internally, which must not be nested inside the running event loop.

If the Alembic config file is not found, startup migration is **skipped with a warning** (rather than failing) telling you to run `pyfly db init` first:

```
pyfly.data.relational.migrations.enabled is true but alembic.ini was not found —
run 'pyfly db init' to create the Alembic environment; skipping migrations.
```

| Config key | Default | Description |
|------------|---------|-------------|
| `pyfly.data.relational.migrations.enabled` | `false` (absent) | Apply migrations on startup when `true`. |
| `pyfly.data.relational.migrations.config` | `alembic.ini` | Path to the Alembic config file. |
| `pyfly.data.relational.migrations.revision` | `head` | Target revision passed to `alembic upgrade`. |

> **Migrations vs. `ddl-auto`:** startup migrations are independent of the `engine_lifecycle` `ddl-auto` schema strategy. For an Alembic-managed database, set `pyfly.data.relational.ddl-auto: none` so the engine does not also create tables from `Base.metadata`, and let migrations own the schema.

**Source:** `src/pyfly/data/relational/migrations.py` (`MigrationRunner`) · `src/pyfly/data/relational/auto_configuration.py` (`MigrationAutoConfiguration`)

---

## Datasource Registry

Every SQLAlchemy engine the application uses is built by one
`DataSourceRegistry`: the primary, its read replica, the named datasources, and the datasources the
framework modules need (event store, snapshots, saga persistence, the PostgreSQL cache). Before
26.09.08 each module built its own engine from a URL, so one database could carry seven pools, only
the primary got the pool settings, and only the primary was disposed on shutdown.

The registry gives every engine the same treatment: the pool settings, `pool.recycle`, the connect
arguments, the SQLite setup and a credential hook. It keeps one engine per database, and it disposes
every engine exactly once when the context stops. `DataSourceAutoConfiguration` exposes it as the
`datasource_registry` bean whenever SQLAlchemy is installed, even with the relational repositories
disabled, because the SQL-backed modules take their datasource from it.

```python
from pyfly.data.relational.datasource_registry import DataSourceRegistry

registry = ctx.get_bean(DataSourceRegistry)
primary = registry.primary                       # DataSource
reporting = registry.get("reporting")
async with reporting.sessionmaker() as session:
    ...
registry.names()                                 # ["primary", "reporting", ...]
registry.replica()                               # the primary's replica DataSource, or None
```

A `DataSource` has these members:

- `name`;
- `url`, whose `str` and `repr` mask the password (`masked_url` renders it masked);
- `engine` and `sessionmaker` (`expire_on_commit=False`);
- `replica`;
- `capabilities`;
- `metadata`, a slot for the framework tables that live on it;
- `customizers`.

The beans you already inject keep their names and types, and each is now a view over the registry:

- `async_engine` and `async_session_factory` are the primary's engine and session factory;
- `routing_session_factory` routes to the primary's replica;
- `named_data_sources` is a live view;
- `db_health_indicator` checks every datasource;
- `query_metrics` covers every engine;
- `engine_lifecycle` leaves disposal to the registry;
- `primary_transaction_manager` (new) is the primary's `SqlAlchemyTransactionManager`, on the
  `async_session_factory` bean: the `transaction_manager_registry` serves the `primary` datasource with
  it (see **Overriding the beans** below).

A relational application with no `pyfly.data.relational.url` **fails at startup**. Before 26.09.08
it silently opened `./app.db` in the working directory. With the `dev` profile active, it falls back
to `sqlite+aiosqlite:///./app.db` and logs a warning.

**Overriding the beans.** `async_engine`, `async_session_factory`, `routing_session_factory` and
`datasource_registry` carry `@conditional_on_missing_bean(..., singletons_only=True)`: declare your
own **singleton** `AsyncEngine`, `async_sessionmaker`, `RoutingSessionFactory` or
`DataSourceRegistry` bean and the framework's backs off (the beans that take them, such as the
health indicator and the engine lifecycle, use yours, and the engine lifecycle disposes an engine
that is not a registry engine). When several beans share a class, the `@primary` one is injected,
and a lookup by that class raises `NoUniqueBeanError` when none is primary. Named and module
datasources are still built by the registry.

Such a bean replaces the application's **primary everywhere**, as a `DataSource` or an
`EntityManagerFactory` bean does in Spring Boot. The `primary_transaction_manager` bean serves the
`primary` datasource on the primary `async_sessionmaker` bean (yours, the one over your engine, or
your registry's), so every unit of work runs on it: `@transactional`, repository calls outside a
transaction, `SessionProvider`, `infrastructure_unit()`, the `AsyncSession` bean, and what maps your
factory to its manager (`reactive_transactional(factory)`, a `_session_factory` attribute). A
`DataSourceRegistry` bean gives `async_engine` and the other relational beans their engine too.

- A session factory over the registry's primary engine (other session options: a session class,
  `expire_on_commit`) keeps the primary's replica, begin options and after-begin customizers; a
  read-only unit on the replica gets your session options there.
- A session factory over an engine of its own, or the factory over your `AsyncEngine` bean, gets
  the capabilities and begin options of that engine's dialect and no after-begin customizers: the
  registry applies those to the datasources it builds.
- The framework disposes what it disposed before: the registry's engines, and an `AsyncEngine`
  bean the registry does not own (through the engine lifecycle). The engine under your session
  factory bean stays yours to dispose.
- Beside a `DataSourceRegistry` bean, the modules that look the registry up by configuration (event
  store, snapshots, saga persistence, the PostgreSQL cache) keep the configuration's registry; the
  context closes both when it stops.
- `primary_transaction_manager` itself is not a replacement point: replace the session factory,
  the engine or the registry.

**Never declare a singleton `AsyncEngine` or `async_sessionmaker` bean for a second database**: it
takes over the primary. Declare the database under `pyfly.data.relational.datasources.<name>` (see
[Multiple Named Datasources](#multiple-named-datasources)) and use `registry.engine("<name>")`,
`registry.session_factory("<name>")` or `NamedDataSources`. Two configurations still **split** the
primary, and a WARNING says so, once per engine and run:

- An engine bean the registry does not own, or a session factory bean over an engine of its own,
  while `pyfly.data.relational.url` is configured: every unit of work runs on your engine, and
  `DataSourceRegistry.primary` keeps the URL for the modules that look the registry up (event store,
  snapshots, saga persistence, the PostgreSQL cache) and for health and pool metrics. It logs
  `relational_engine_not_in_registry`. Leave the URL unset when your engine is the only primary.
- A session factory bean over a named datasource or a replica of the registry: the `primary` units
  and the units that name that datasource are two units on one database, and
  `DataSourceRegistry.primary` keeps the URL. It logs `relational_primary_on_named_datasource`.

A session factory over the registry's own primary engine is no split. Driver arguments, pool
settings and the credentials provider are all configurable on the registry's own primary, so an
engine bean is rarely needed.

A request- or refresh-scoped bean of one of these types is a **second database**, not a
replacement: the auto-configured beans stay, and they are the `@primary` candidates of their type.
An injection by type (`AsyncEngine`, `async_sessionmaker[AsyncSession]`), the `AsyncSession` bean,
the routing factory and the repositories keep the primary; inject the scoped bean by name:

```python
@configuration
class TenantSessions:
    @bean(scope=Scope.REQUEST)
    def tenant_sessions(self, registry: DataSourceRegistry) -> async_sessionmaker[AsyncSession]:
        # one named datasource per tenant: pyfly.data.relational.datasources.<tenant>
        return registry.session_factory(str(RequestContext.current().get("tenant")))


class TenantReader:
    def __init__(
        self, sessions: Annotated[async_sessionmaker[AsyncSession], Qualifier("tenant_sessions")]
    ) -> None:
        self.sessions = sessions
```

**Closing.** The context closes the registry **last** when it stops: after the consumers drained,
after every `@pre_destroy` and after every lifecycle bean stopped, so the last writes of the
application still have their datasource (see the stop() lifecycle in the dependency-injection
guide). `close()` disposes the engines concurrently and waits for them at most
`pyfly.data.relational.datasource_registry.CLOSE_TIMEOUT` seconds (5 by default, or
`close(timeout=...)`): a dispose that
is still waiting is on a database that stopped answering, and it is cancelled while the connections
left idle in its pool are terminated (their sockets closed, nothing sent), so `ctx.stop()` ends on
time. When `pyfly.context.shutdown-timeout` is shorter and the stop cancels the close first, those
connections are terminated all the same. From the moment it is closed, the registry's engines
**refuse to connect**: using one raises
`DataSourceConfigurationError` instead of silently opening a pool that nobody would dispose, and
the db health indicator of a closed registry answers `OUT_OF_SERVICE` without touching them. A
restarted context builds a new registry.

A connection that is **in use** while the registry closes (a request or a readiness probe in flight
during `ctx.stop()`), or that a connect in flight opens in the old pool just after, finishes its
work and is **closed when it is returned**. `AsyncEngine.dispose()` alone closes the idle
connections only: a connection returned later went back into the disposed pool and stayed open (on
PostgreSQL, in `pg_stat_activity`) until the garbage collector found that pool. The hook,
`close_connections_on_return(engine)` (from `pyfly.data.relational.datasource_registry`), closes a
connection returned to a pool the engine no longer uses, and leaves the current pool alone: the
engine pools as before after any number of disposes (an engine handed to a restarted context,
shared with a second one, an in-memory SQLite database on a `StaticPool`). The registry installs it
on every engine it builds, the context on every `AsyncEngine` a `@bean` method returns, and the
engine lifecycle on the application's engine it disposes. For an engine you create and dispose
yourself, call it **when you create the engine**: it adds pool listeners, and adding them while a
connection is connecting (asyncpg awaits inside the connect event) breaks that connect.

The registry belongs to the **configuration object** (`DataSourceRegistry.for_config(config)`): two
contexts built on one `Config` share it, and stopping one closes it for both, so the other's engines
refuse to connect from then on. Give each context its own `Config`.

### Configuration Reference

Every key is read for its exact name, so `${...}` placeholders resolve and a `PYFLY_*` environment
variable wins for every key, named datasources included. Values are cast with Config's truthy set
(`true/false`, `yes/no`, `on/off`, `1/0`). A value that is not a boolean or a number where one is
expected raises at startup and names the key. Before 26.09.08, `bool("false")` turned SQL echo on.

A named datasource can also be declared in the environment alone, with no YAML entry:
`PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_URL` registers `analytics`, and
`PYFLY_DATA_RELATIONAL_DATASOURCES_ANALYTICS_POOL_SIZE` sizes its pool. A name declared this way is
lower-case and has no dash, since every underscore reads as a key separator; declare a dashed name
such as `event-store` in YAML, where the environment can still override its keys.

| Key | Default | Description |
|-----|---------|-------------|
| `pyfly.data.relational.url` | — (required) | Primary datasource URL. |
| `pyfly.data.relational.echo` | `false` | Log SQL: `true`, `false`, or `debug` (also logs result rows). |
| `pyfly.data.relational.ddl-auto` | `create` | Schema strategy of `engine_lifecycle` (`create`, `create-drop`, `none`). |
| `pyfly.data.relational.pool.size` | SQLAlchemy's (5) | Pool size (queue pools only). |
| `pyfly.data.relational.pool.max-overflow` | SQLAlchemy's (10) | Connections beyond `size`. |
| `pyfly.data.relational.pool.timeout` | SQLAlchemy's (30) | Seconds to wait for a connection. |
| `pyfly.data.relational.pool.recycle` | `1800` | Replace a pooled connection after this many seconds (`-1` never). |
| `pyfly.data.relational.pool.pre-ping` | `false` | Test every checkout with a round trip. |
| `pyfly.data.relational.connect-args.*` | — | Passed to the driver verbatim, for example asyncpg `statement_cache_size: 0` behind pgbouncer, `server_settings`, SSL or timeouts. |
| `pyfly.data.relational.sqlite.foreign-keys` | `true` | `PRAGMA foreign_keys=ON` on every connection. |
| `pyfly.data.relational.sqlite.journal-mode` | `WAL` | Journal mode of file databases. |
| `pyfly.data.relational.sqlite.synchronous` | `NORMAL` | `PRAGMA synchronous` of file databases. |
| `pyfly.data.relational.sqlite.busy-timeout` | `5000` | Milliseconds to wait for a lock. It is left alone when the URL or `connect-args` set sqlite3's `timeout`. |
| `pyfly.data.relational.read-replica.url` | — | The primary's read replica. |
| `pyfly.data.relational.datasources.<name>.*` | inherited | A named datasource. It takes `url`, `echo`, `pool.*`, `connect-args.*`, `sqlite.*` and `read-replica.url`. |
| `pyfly.data.relational.health.timeout` | `2` | Seconds each `db` health check may take. |

Why pre-ping is off by default: it adds a round trip to every checkout (+0.675 ms on asyncpg, which
nearly doubles a short unit of work). These cover the same failures without that cost:

- `pool.recycle` bounds a connection's age;
- SQLAlchemy invalidates the pool when it detects a disconnect.

Turn pre-ping on where connections are closed while they sit idle in the pool: by the server's own
idle timeout (PostgreSQL `idle_session_timeout`, MySQL `wait_timeout`), by a restart or a failover
that closes them, or by a middlebox that answers the packets of a flow it has expired with a reset,
such as an AWS NAT gateway, an Azure NAT Gateway, or an Azure Load Balancer with TCP reset on idle
enabled. The ping then fails at once and SQLAlchemy replaces the connection before the application
gets it; without pre-ping, the first statement on that connection fails.

Pre-ping does not help when a middlebox drops the packets of flows it has expired without a reset,
such as an Azure Load Balancer without TCP reset on idle (its default) or a stateful firewall that
drops packets of flows it no longer tracks. The ping then waits like any statement until the
connection is lost: when the kernel gives up retransmitting (about 15 minutes on Linux), or never
behind a proxy that keeps acknowledging. asyncpg's `command_timeout` does not bound it: the timed-out
statement leaves a cancel request pending, and asyncpg then waits for the server's answer on the dead
socket with no timeout, even once the connection is lost.

In both cases, set `pool.recycle` below the middlebox's idle timeout: 350 s on an AWS NAT gateway,
and 4 minutes by default on an Azure NAT Gateway and on an Azure Load Balancer, for example, all below
the 1800 s default. A connection idle for longer than that timeout is then always older than
`pool.recycle`, so the checkout replaces it instead of using it, with no reset for a pre-ping to
absorb and no dropped packets to wait on; on asyncpg the replacement waits up to 2 s for the old
connection to close. Or keep the flows alive with TCP keepalives more frequent than that timeout: on
PostgreSQL, `connect-args.server_settings.tcp_keepalives_idle` (seconds) makes the server send them.

On PostgreSQL, every connection carries `pyfly.app.name` as its `application_name`, which makes it
visible in `pg_stat_activity`. Set `connect-args.server_settings.application_name` to override it.

The keys `pyfly.data.url`, `pyfly.data.echo` and `pyfly.data.pool-size` used to be documented, but
nothing read them. They are now **deprecated aliases** of `url`, `echo` and `pool.size`: they are
honored when the `relational` key is absent, with a warning.
`pyfly.data.relational.pool-size` is likewise an alias of `pool.size`.

### SQLite Setup

SQLite's defaults, and pysqlite's, are wrong for an application database. Every SQLite engine the
registry builds therefore gets the following setup:

- **Foreign keys are enforced.** `PRAGMA foreign_keys=ON` runs on every connection. Orphan rows are
  rejected and `ON DELETE CASCADE` runs, as on PostgreSQL and MySQL.
- **File databases run in WAL mode with `synchronous=NORMAL`.** Readers run beside a writer. In-memory
  databases keep `StaticPool` and are never recycled. Every session shares that one connection, so
  sessions are not isolated from each other: a plain session that begins while another one's
  transaction is open joins it, and once that transaction ends, the joined session's later statements
  run in autocommit until it begins again. Units of work do not share it: a unit that begins while
  another unit holds the connection (a concurrent call, `REQUIRES_NEW`, a repository call while
  `stream_all` iterates outside a transaction) fails at once with `IllegalTransactionStateError`, and a
  cancelled unit rolls back instead of discarding the connection (which would drop the database). Test
  transactional and concurrent behavior on a file database.
- **The engine emits `BEGIN` itself.** This is SQLAlchemy's documented pysqlite/aiosqlite recipe.
  The driver otherwise defers `BEGIN` until the first write, so the reads of a read-modify-write run
  outside the transaction and a concurrent update is lost. Now two such transactions serialize: one of
  them fails with `database is locked` instead of both committing.
- **A unit that will write can start with `BEGIN IMMEDIATE`.** It takes the write lock up front and
  waits `busy-timeout` for it, instead of failing on the lock upgrade later. Ask for it before the first
  statement with `session.connection(execution_options=datasource.begin_options(read_only=False))`;
  `begin_options` returns `{"pyfly_sqlite_begin": "IMMEDIATE"}` on SQLite and `{}` elsewhere. A
  transaction that does not ask starts with a plain `BEGIN`. The transaction manager asks for it on every
  write unit (`@transactional` and a repository's write auto unit); read-only units start with `BEGIN`.
- **SQLite has one writer.** A write unit holds the write lock from its `BEGIN IMMEDIATE`. A new write
  unit that would wait for the lock of a unit the same task keeps open (it suspended it with
  `REQUIRES_NEW`, or a write under `NOT_SUPPORTED`) fails at once with `IllegalTransactionStateError`
  instead of waiting `busy-timeout` for itself. A read under a suspended write unit works (WAL).
  A child task is not refused that way: a task started inside a write unit (an `asyncio.gather()` child)
  whose step opens a write unit of its own (`REQUIRES_NEW`, or a write under `NOT_SUPPORTED`) waits
  `busy-timeout` for the lock its parent's unit holds, and fails with `OperationalError: database is
  locked` at `BEGIN IMMEDIATE` while the parent waits for it. The unit cannot tell a parent that awaits
  the child from one that commits meanwhile (a task left running, whose wait then succeeds). On SQLite,
  let such steps join the parent's unit (`REQUIRED`: the children are serialized on it), run them after
  the parent's unit, or put them on another datasource.
- **An explicit `BEGIN IMMEDIATE` statement no longer works.** `session.execute(text("BEGIN IMMEDIATE"))`
  inside `session.begin()` was the way to take the write lock under pysqlite's deferred `BEGIN`. On a
  registry engine the transaction's `BEGIN` has already run, so SQLite answers "cannot start a
  transaction within a transaction". Call `await begin_immediate(session)` from
  `pyfly.data.relational.dialect_customizers` instead: it sets the execution option on a registry
  engine and executes `BEGIN IMMEDIATE` on an engine you built yourself. The model administration's
  SQLAlchemy provider uses it.

### Module Datasources

The per-module URL keys are aliases that resolve through the registry:

- `pyfly.eventsourcing.store.url`;
- `pyfly.eventsourcing.snapshot.url`;
- `pyfly.transactional.persistence.sqlalchemy.url`;
- `pyfly.cache.postgres.url`;
- `pyfly.scheduling.lock.url`.

Each resolves the same way:

- **No URL** means the primary datasource.
- **A URL identical to a registered datasource's** (the password aside, SQLite paths made absolute)
  reuses that datasource's engine.
- **Another URL** registers a named datasource that gets the same treatment (pool, connect arguments,
  SQLite setup, credential hook). The name is `event-store`, `snapshot-store`,
  `transactional-persistence`, `cache` or `scheduling-lock`.

The saga persistence, the SQL cache and the scheduler lock also take a `datasource` key beside the URL
(`pyfly.transactional.persistence.sqlalchemy.datasource`, `pyfly.cache.postgres.datasource`,
`pyfly.scheduling.lock.datasource`) that names a datasource of the registry instead; setting both is an
error. They look the datasource up in the context's `DataSourceRegistry` bean, so an application's own
registry bean is the one they use.

A module with no URL and no primary fails with an error naming both keys. It used to fall back to
`./app.db`, or for the cache to `localhost:5432/cache`.

```python
datasource = registry.resolve(config.get("pyfly.myfeature.url"), name="my-feature",
                              url_key="pyfly.myfeature.url")
```

### Framework Tables

The tables the framework keeps in an application's database (`pyfly_orchestration_state`,
`pyfly_cache_entries`, `pyfly_locks` and `pyfly_users`) are SQLAlchemy Core tables on one `MetaData`,
`pyfly.data.relational.framework_schema.framework_metadata`, with portable
types: bounded `Unicode` keys, `UtcTimestamp` instants (UTC with microseconds on every backend, aware in
Python: `TIMESTAMPTZ` on PostgreSQL, `DATETIME(6)` on MySQL and MariaDB), `LONGTEXT`/`LONGBLOB` payloads on
MySQL and MariaDB, and a naming convention for indexes.

List it in Alembic's `target_metadata` so autogenerate migrates the framework tables and never drops them:

```python
# migrations/env.py
from pyfly.data.relational.framework_schema import framework_metadata
from pyfly.data.relational.sqlalchemy.entity import Base

target_metadata = [Base.metadata, framework_metadata]
```

A store creates its tables when it starts if `ddl-auto` is `create` (the default), `create-drop` or `update`;
with `none`, `validate` or any other value it only checks them, and fails the startup naming each missing table
or column (`FrameworkSchemaError`). When it may create them, it also adds the indexes a table an earlier release
created is missing (the cache's `expires_at` index); on PostgreSQL with `CREATE INDEX CONCURRENTLY`, so the nodes
still running the earlier release keep writing to the table during a rolling deploy. A concurrent build that is
interrupted leaves an invalid index behind: rebuild it with `REINDEX INDEX CONCURRENTLY <name>`. The same helper
is public:

```python
from pyfly.data.relational.framework_schema import ensure_tables, locks

await ensure_tables(registry.primary, locks, create=False)   # check only: migrations own the schema
```

Stores write with `pyfly.data.relational.upsert`, which sends each dialect its own statement:
`upsert(executor, table, values, key=[...], where=...)` (`ON CONFLICT ... DO UPDATE` on PostgreSQL and
SQLite, `ON DUPLICATE KEY UPDATE` on MySQL and MariaDB, `UPDATE` then `INSERT` in a savepoint elsewhere),
`insert_if_absent(executor, table, values, key=[...], replace_where=...)` (returns whether it wrote), and
`take_over(...)`. The executor is a unit of work's session (from `infrastructure_unit()`) or a connection.

### After-Begin Customizers

An `AfterBeginCustomizer` runs inside a transaction on a datasource, right after `BEGIN`. Use it for a
tenant GUC, a `SET LOCAL statement_timeout` or a `search_path`. Declare it as a
bean and it applies to every datasource and its replica. To limit it, give the class a `datasources`
attribute, or register it with `registry.add_customizer(customizer, datasource="name")`. Customizers
run in `@order` order. An exception aborts the unit.

A customizer bean, like a `DataSourceCredentialsProvider` bean, must be a **singleton**: the registry
keeps it for its whole life. A `TRANSIENT`, `REQUEST` or refresh-scoped one is not registered, and a
`datasource_spi_bean_not_singleton` warning names it. Registering one at each creation used to run a
request's customizer in the units of work of every later request (the last tenant won) and to keep an
evicted credentials provider answering with the old password. A singleton reads the request (a
`ContextVar`, as below) or the live configuration when it is called.

```python
from contextvars import ContextVar
from sqlalchemy import text
from pyfly.container import component

current_tenant: ContextVar[str | None] = ContextVar("current_tenant", default=None)


@component
class TenantGuc:
    async def after_begin(self, connection, datasource) -> None:
        tenant = current_tenant.get()
        if tenant is not None and datasource.capabilities.dialect == "postgresql":
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant}
            )
```

The transaction manager runs the customizers right after `BEGIN` in every unit it opens:
`@transactional` units, repository auto units, `SessionProvider` and `infrastructure_unit()` units,
also when an application's session factory bean over the registry's engine is the primary. A session
factory over an engine the registry did not build has none. A
datasource with customizers gets transactional read auto units on PostgreSQL instead of `AUTOCOMMIT`
ones, so a transaction-local setting applies to the read. A transaction you open yourself runs them with
`await datasource.run_after_begin(session)` (or `run_after_begin(datasource, session)`).

### Credential Rotation

A `do_connect` hook asks for the user name and password every time the pool opens a connection. It
takes them from the first of:

- a `DataSourceCredentialsProvider` bean (`datasource_credentials(datasource) -> (user, password) | None`),
  which fits IAM tokens or a secrets client. It is asked with the datasource's qualified name:
  `primary`, a named datasource such as `reporting`, or `<name>.replica` for a read replica
  (`primary.replica`). A replica is asked apart from its primary because it usually runs on another
  host (an IAM token is scoped to one) and often logs in as a read-only role. Return `None` for it to
  keep the replica's configured user;
- the **live** configuration: the datasource's URL key is read again, so a `${DB_PASSWORD}` placeholder
  or a refreshed configuration takes effect.

A rotated password therefore reaches new connections without a restart. On a configuration refresh
(`POST /actuator/refresh`), every pool that still holds a connection opened with the old password is
soft-evicted, including a pool that already opened connections with the new one between the rotation
and the refresh (a pool growing under load):

- idle connections are closed at once;
- a connection in use finishes its work and is closed when it is returned, instead of going back to a
  pool;
- new connections authenticate with the new password.

`pool.recycle` bounds the age of every other connection.

The hook changes the driver's connect parameters. A dialect that takes its credentials from a
positional connection string (the ODBC dialects, such as `mssql+aioodbc`) is not rotated this way;
rebuild its connection string or restart the application.

### Capabilities

`DataSource.capabilities` tells the dialect-gated accelerators what they may use:

| Field | Meaning |
|-------|---------|
| `dialect`, `driver` | For example `postgresql` / `asyncpg`, or `mariadb` / `asyncmy` (MariaDB is detected from the server). |
| `supports_savepoints` | SQLite (with the recipe), PostgreSQL, MySQL, MariaDB, SQL Server and Oracle. |
| `supports_returning`, `insert_returning`, `update_returning`, `delete_returning` | Final after the first connection. For example, MariaDB 11 has `INSERT ... RETURNING` and MySQL 8 has none. |
| `fast_autocommit_reads` | `True` only on PostgreSQL. |
| `multiple_active_results` | Whether a statement can run on a connection while a streamed result is open on it. `False` on MySQL and MariaDB: the unit of work then refuses other statements until the stream is exhausted or closed. |
| `isolation_levels` | The driver's levels. asyncpg has no `READ UNCOMMITTED`; SQLite has only `SERIALIZABLE` and `READ UNCOMMITTED`. |
| `max_in_params` | The largest IN list a statement may bind. |

**Source:** `src/pyfly/data/relational/datasource_registry.py` · `src/pyfly/data/relational/dialect_customizers.py` · `src/pyfly/config/properties/data.py` · beans: `DataSourceAutoConfiguration`

---

## Read/Write Routing (Read Replicas)

PyFly can route read-only work to a database **read replica** while keeping writes on the primary — the equivalent of Spring's `AbstractRoutingDataSource` driven by `@Transactional(readOnly = true)`. Routing is **opt-in**: with no replica configured, every session goes to the primary, so behavior is unchanged for existing apps.

```python
from pyfly.data.relational.routing import RoutingSessionFactory, read_only, is_read_only
```

### Enabling a Replica

Set the replica URL under `pyfly.data.relational.read-replica.url`. The [datasource registry](#datasource-registry) builds the replica's engine with the primary's settings (pool, connect arguments, credential hook) and `routing_session_factory` routes to its `async_sessionmaker`:

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

## Multiple Named Datasources

In addition to the primary datasource, PyFly can configure any number of **secondary datasources** — the equivalent of Spring declaring multiple `DataSource` beans. Each named datasource gets its own engine and `async_sessionmaker`, kept separate from the primary's dedicated beans.

Declare each one under `pyfly.data.relational.datasources.<name>`. Only `url` is required. Every other
key (`echo`, `pool.*`, `connect-args.*`, `sqlite.*`, `read-replica.url`) is inherited from the primary
and can be overridden; the connect arguments are inherited only when the driver is the same. Every key
is read like the primary's, so `${...}` placeholders resolve and a `PYFLY_*` override such as
`PYFLY_DATA_RELATIONAL_DATASOURCES_REPORTING_URL` wins. Keep secrets out of the file. The name
`primary` is reserved.

```yaml
pyfly:
  data:
    relational:
      url: postgresql+asyncpg://app:${DB_PASSWORD}@primary:5432/app   # primary (unchanged)
      datasources:
        reporting:
          url: postgresql+asyncpg://reports:${REPORTING_PASSWORD}@reporting:5432/reports
          pool:
            size: 3
        analytics:
          url: postgresql+asyncpg://etl:${ANALYTICS_PASSWORD}@analytics:5432/warehouse
```

The `named_data_sources` bean is a live `NamedDataSources` view over the [datasource registry](#datasource-registry). It also lists the datasources a module registers. Inject it and call `.get("<name>")` to retrieve that datasource's `async_sessionmaker`:

```python
from pyfly.container import service
from pyfly.data.relational.named_datasources import NamedDataSources
from sqlalchemy import text


@service
class ReportingService:
    def __init__(self, datasources: NamedDataSources) -> None:
        # async_sessionmaker for the "reporting" datasource
        self._reporting = datasources.get("reporting")

    async def daily_total(self) -> int:
        async with self._reporting() as session:        # AsyncSession on the reporting DB
            result = await session.execute(text("SELECT COUNT(*) FROM orders"))
            return int(result.scalar_one())
```

### NamedDataSources

| Member | Returns | Description |
|--------|---------|-------------|
| `get(name)` | `async_sessionmaker[AsyncSession]` | Session factory for `name`; raises `KeyError` if unknown. |
| `names()` | `list[str]` | Sorted names of all configured secondary datasources. |
| `dispose()` | `None` (await) | Disposes the engines a hand-built instance was given. On the auto-configured bean it does nothing: the registry disposes each of its datasources exactly once when the context stops. |
| `name in datasources` | `bool` | Whether a datasource is configured (`__contains__`). |
| `len(datasources)` | `int` | Number of configured secondary datasources. |

The primary datasource keeps its own `async_session_factory` / `async_session` beans and is **not** part of this registry. When no `datasources` are configured, the bean is still registered but empty (`len(...) == 0`), so existing apps are unaffected.

**Source:** `src/pyfly/data/relational/named_datasources.py` · bean: `RelationalAutoConfiguration.named_data_sources` *(v26.06.48)*

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

### Transaction Management with @transactional

`@transactional` gives Spring's `@Transactional` semantics on every backend. It works bare or
parametrized, on an `async def` method or function, or on a class (every public `async def` defined on
it; a method's own settings win over the class's).

#### Basic Usage

```python
from pyfly.container import service
from pyfly.data.relational.sqlalchemy import Isolation, Propagation, transactional


@service
class OrderService:
    def __init__(self, repo: OrderRepository, audit: AuditService) -> None:
        self.repo = repo
        self.audit = audit

    @transactional
    async def create_order(self, order: Order) -> Order:
        saved = await self.repo.save(order)
        await self.audit.log(f"order {saved.id} created")   # REQUIRES_NEW: commits on its own
        return saved

    @transactional(isolation=Isolation.SERIALIZABLE, read_only=True)
    async def generate_report(self) -> Report:
        ...


@transactional(datasource="reporting")            # class level: every public async method
class ReportingService:
    async def record(self, row: ReportRow) -> None: ...

    @transactional(propagation=Propagation.MANDATORY)   # keeps datasource="reporting"
    async def record_inside(self, row: ReportRow) -> None: ...
```

At decoration time it raises `TypeError` for a sync function and for an async generator (a transaction
cannot safely span iteration; use `TransactionTemplate` around the loop).

#### Which transaction manager runs a call

Resolved per call, in this order:

1. `manager=` (a `TransactionManager` or a datasource name) or `datasource="name"`, on the method or on
   a class-level `@transactional`;
2. the legacy attributes: `self._session_factory` (an `async_sessionmaker`, mapped to its registry
   datasource; a factory you built yourself gets a manager of its own) and `self._motor_client`. A
   service that exposes **both** raises `IllegalTransactionStateError` unless a datasource is named:
   one arm would commit while the other rolled back;
3. the running application context's default datasource (`primary`). A service with no factory
   attribute works, and so does a plain function.

A unit on one datasource never satisfies a join on another: a `REQUIRED` call on `reporting` inside a
unit on `primary` begins `reporting`'s own unit (there is no two-phase commit between them).

#### Propagation Types

| Propagation | A unit is bound for the datasource | None is bound |
|------------|------------------------------------|---------------|
| `REQUIRED` (default) | join it | begin a new unit |
| `REQUIRES_NEW` | suspend it, begin a new unit, resume on exit | begin a new unit |
| `NESTED` | savepoint in it (`begin_nested`) | begin a new unit |
| `SUPPORTS` | join it | run without one (repository calls get auto units) |
| `NOT_SUPPORTED` | suspend it, run without one | run without one |
| `MANDATORY` | join it | `IllegalTransactionStateError` |
| `NEVER` | `IllegalTransactionStateError` | run without one |

#### Rollback rules and rollback-only

- Any `Exception` rolls back by default; a `BaseException` that is not an `Exception` (cancellation)
  always rolls back.
- `rollback_for` **adds** rollback rules and `no_rollback_for` adds commit rules; the most specific
  rule wins (the class closest to the exception in its MRO), and a tie rolls back. Narrowing
  `rollback_for=(PaymentError,)` does not make a `KeyError` commit.
- A participant (a joined call) that exits with an exception its rules roll back marks the unit
  **rollback-only**, and so does any failed statement. When the outermost boundary then completes
  normally, it rolls back and raises `UnexpectedRollbackError`. To try a step and carry on after it
  fails, use `NESTED`: its failure rolls back to the savepoint only.
- A statement that fails inside a savepoint the application opened itself does not mark the unit when
  that savepoint rolls back: after `ROLLBACK TO SAVEPOINT` the transaction is healthy on every backend.
  The SQLAlchemy idiom works as it does without PyFly, on the repository's `_session`, an injected
  `AsyncSession` or `SessionProvider.current()`, with or without a flush inside the block:

  ```python
  @transactional
  async def import_tags(self, names: list[str]) -> None:
      session = self.tags._session
      for name in names:
          try:
              async with session.begin_nested():
                  session.add(Tag(name=name))  # or: await session.merge(Tag(name=name))
          except IntegrityError:
              pass  # a duplicate: its savepoint rolled back, the unit goes on
  ```

  Without a flush inside the block, the insert runs in the flush that releasing the savepoint does at the
  end of the block; when it fails, SQLAlchemy rolls the savepoint back before the `IntegrityError` reaches
  the `except`, and the failure went away with it. The same holds for `await savepoint.commit()` followed
  by `await savepoint.rollback()` when the commit fails.

  A failure caught while its savepoint stays open counts when the savepoint is released (it moves to the
  enclosing savepoint, or marks the unit), and when the unit completes with the savepoint still open (the
  unit rolls back with `UnexpectedRollbackError`). Inside `NESTED`, it rolls the `NESTED` scope back to
  its own savepoint instead. A savepoint's `SAVEPOINT`, `RELEASE` and `ROLLBACK TO` run under the unit's
  operation guard.
- A `NESTED` scope that leaves changes pending (an added entity, a changed one) has them flushed when its
  savepoint is released. When that flush fails (a duplicate), the scope rolls back to its savepoint, its
  caller gets the failure, and the outer unit is not marked: it goes on, exactly as when the failure is
  raised inside the scope.
- A failure that leaves the transaction healthy (a `before_flush` hook that refuses a flush before it
  writes anything, caught by the application) does not mark the unit, and its connection goes back to the
  pool as usual.
- A `no_rollback_for` exception on a unit whose transaction is already dead rolls back and re-raises the
  original exception (never `PendingRollbackError`).

#### Isolation, read-only and timeout

- **Isolation** is applied with `session.connection(execution_options={"isolation_level": ...})` before
  the first statement of a new unit, and validated against the dialect: an unsupported level (asyncpg
  has no `READ UNCOMMITTED`; SQLite has only `SERIALIZABLE` and `READ UNCOMMITTED`) raises
  `IllegalTransactionStateError` at begin. A joining call's isolation is ignored.
- **`read_only=True`** routes a new unit to the datasource's replica when one is configured, sets
  `session.info["read_only"]`, keeps `is_read_only()` true inside the call, refuses every ORM write
  (`IllegalTransactionStateError` from a `before_flush` guard) and every Core `insert()`/`update()`/
  `delete()` before it is sent, and adds the dialect hint: `BEGIN READ ONLY` on PostgreSQL,
  `SET TRANSACTION READ ONLY` on MySQL and MariaDB (which also refuses a raw `text()` write; SQLite has
  no read-only transaction).
- **`timeout=`** (seconds) bounds a new unit's body with `asyncio.timeout`: on expiry the unit rolls
  back and `TransactionTimedOutError` (a `TimeoutError`) is raised. On PostgreSQL the unit also gets
  `SET LOCAL statement_timeout`, so a stuck statement is cancelled on the server. A participant cannot
  extend its unit's deadline.

#### Synchronizations

```python
from pyfly.data.transaction import after_commit, register_synchronization

@transactional
async def place(self, order: Order) -> None:
    await self.orders.save(order)
    await after_commit(lambda: self.events.publish(OrderPlaced(order.id)))
```

`register_synchronization(sync)` takes a `TransactionSynchronization` (`before_commit(read_only)`,
`before_completion()`, `after_commit()`, `after_completion(status)`; subclass
`TransactionSynchronizationAdapter`). `before_commit` runs inside the unit and a failure there rolls it
back. `after_commit` and `after_completion` run once the connection is released, outside any transaction
(repository calls there get auto units); a failure there is logged and counted in
`pyfly.tx.synchronization.failures`, and never turns a committed unit into a failure. Outside a
transaction (a `SUPPORTS` or `NOT_SUPPORTED` boundary with no unit included) `after_commit(callback)` and
`on_phase(...)` run the callback at once, while `register_synchronization()` raises
`IllegalTransactionStateError`: there is no unit to register on, and its callbacks are coroutines it cannot
run from a plain call. Check `is_transaction_active()` first, or use `after_commit()`.

#### Cancellation and commit outcome

Commit, rollback and session close run shielded, and a cancellation that arrived meanwhile is re-raised
afterwards, so a client that disconnects mid-transaction (Starlette cancels the request through an anyio
scope) never returns a poisoned or leaked connection to the pool. A commit whose connection fails while
`COMMIT` is in flight raises `CommitOutcomeUnknownError`: the unit may have committed. Never retry it
blindly; `@retry` does not (see [Resilience](resilience.md#retries-and-transactions)).

A cancellation that lands while a statement is in flight can come back as a driver error: an anyio scope
cancels SQLAlchemy's own cleanup of the interrupted statement too, and aiosqlite then raises
`ValueError('Connection closed')`, asyncmy `InterfaceError('Cancelled during execution')`. When a cancel
request arrived while the operation (or the unit) ran, such an error ends the operation, and the unit, as
the cancellation it stood in for: the unit's connection is discarded, `CancelledError` is raised (chained
from the driver's error), and the cancel scope that fired catches it, `wait_for` times out, or the unit's
own `timeout` raises `TransactionTimedOutError`.

Cleanup code that runs outside the cancelled unit is not affected: a task stays "being cancelled"
(`Task.cancelling() > 0`) throughout `except CancelledError:`, `finally:` and anyio's
`with CancelScope(shield=True):` cleanup, and the data access done there only counts cancel requests that
arrive after it started. A duplicate saved in a compensation handler raises `IntegrityError`, a
`@transactional` audit call that raises a business exception in shielded cleanup raises that exception,
and the rest of the cleanup runs:

```python
async def handle(self, request: Request) -> None:
    try:
        await self.process(request)
    finally:
        with anyio.CancelScope(shield=True):   # runs even when the client disconnected
            try:
                await self.ledger.release_hold(request.id)   # @transactional
            except HoldAlreadyReleasedError:
                pass
            await self.audit.record(request.id)
```

Cleanup inside the cancelled unit itself (a `finally:` block in the `@transactional` body) keeps the
failures of its statements: a duplicate saved there raises `IntegrityError`, because the operation that
ran the statement judged it after the cancel request had arrived. Any other exception raised there while
the unit is being cancelled ends the unit as cancelled instead, with `CancelledError` chained from it
(`__cause__`): the unit cannot tell it from a driver error raised outside a guarded statement (on a raw
`AsyncConnection`), which must end as the cancellation so that the cancel scope that fired catches it.
Such an exception is logged at `WARNING` as `transaction_error_replaced_by_cancellation`, with its
traceback. Cleanup whose own exceptions the caller must see belongs outside the cancelled unit, as above.

The judgment follows `raise ... from`: an exception raised from a statement's failure (its `__cause__`
chain reaches the failure the operation judged) keeps its type, and one raised while merely handling it
(only `__context__` links them) does not:

```python
finally:
    try:
        await self.holds.save(Hold(order_id))
    except IntegrityError as error:
        raise HoldExistsError(order_id) from error   # raised as HoldExistsError
    # except IntegrityError:
    #     raise HoldExistsError(order_id)            # ends as CancelledError, logged at WARNING
```

On SQLite a discarded connection rolls back on aiosqlite's worker thread before its handle closes, and a
statement still running there is interrupted, so a cancelled unit never leaves `BEGIN IMMEDIATE`'s write
lock held by a half-closed handle (which would make every other writer wait `busy_timeout` and fail with
"database is locked" until the garbage collector ran).

Under anyio (Starlette), a cancel that hits a statement in flight also makes SQLAlchemy's pool log
`Exception terminating connection` at `ERROR`, with the `CancelledError` traceback: SQLAlchemy's own
cleanup of the interrupted statement is cancelled again while it terminates the connection. It reports
the discard of that connection; the pool is healthy afterwards, and the unit ends as described above.

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
    pass  # delete_by_id()/delete() set deleted_at, find methods exclude deleted entities
```

| Method | Behavior |
|--------|----------|
| `delete_by_id(id)` | Sets `deleted_at` (soft delete); returns `None` |
| `delete(entity)` | Sets `deleted_at` on the given entity (soft delete); returns `None` |
| `find_by_id(id)` | Excludes soft-deleted entities |
| `find_all()` | Excludes soft-deleted entities |
| `find_all(pageable)` | Excludes soft-deleted entities; counts total, applies the `Pageable`'s sort, slices with `LIMIT`/`OFFSET`, returns `Page[T]` |
| `find_all_by_id(ids)` | Excludes soft-deleted entities |
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

It runs for every repository the container creates, whatever its scope: a singleton, a `@lazy` one
first resolved during startup, and a `TRANSIENT`, `REQUEST` or custom-scoped one (until 26.09.07
those kept their stubs, and `find_by_*` answered `None`). It declares `@order(HIGHEST_PRECEDENCE +
100)`, ahead of the AOP post-processor, so an aspect on `repository.*.*` wraps the compiled
derived and `@query` methods instead of being replaced by them.

### How It Works

The `after_init(bean, bean_name)` method:

1. Checks if the bean is an instance of `Repository`. If not, it is returned unchanged.
2. Gets the entity type from `bean._model`.
3. Iterates over all attributes defined on the bean's class (not inherited from `Repository`).
4. For `@query`-decorated methods: compiles them via `QueryExecutor.compile_query_method()` and replaces the stub with a wrapper that runs on `bean._session`. Call them with keyword arguments.
5. For derived query methods (`find_by_*`, `count_by_*`, `exists_by_*`, `delete_by_*`): checks if the method is a stub, parses the method name via `QueryMethodParser.parse()`, compiles it via `QueryMethodCompiler.compile()`, and replaces the stub with a wrapper.
6. Every compiled wrapper is a repository operation like the inherited methods: it joins the current unit of work or runs in an auto unit (a read unit for `find_by_`, `count_by_`, `exists_by_` and `SELECT` queries, a write unit otherwise).
7. Binds the repository to the application context's transaction managers, so two contexts in one process never share units.

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
        await self._repo.delete_by_id(UUID(product_id))

    async def count_in_category(self, category: str) -> int:
        return await self._repo.count_by_category(category)
```

---

## See Also

- [Data Module Guide](data.md) — Generic commons: repository ports, pagination, query parsing, entity mapping, extensibility
- [Data Document Guide](data-document.md) — MongoDB adapter
- [SQLAlchemy Adapter Reference](../adapters/sqlalchemy.md) — Setup, configuration, adapter-specific features
