# Data Document — MongoDB Adapter

> **Package:** `pyfly.data.document.mongodb`
> **Commons:** [`pyfly.data`](data.md) — shared ports, pagination, query parsing, entity mapping
>
> This guide covers the **MongoDB adapter** for document databases (Beanie ODM on pymongo's async client). For generic data concepts shared across all adapters (repository ports, `Page`/`Pageable`/`Sort`, `QueryMethodParser`, `Mapper`, extensibility), see the [Data Module Guide](data.md). For relational databases, see the [Data Relational Guide](data-relational.md).
>
> **Hexagonal by design:** your services depend on [`RepositoryPort[T, ID]`](data.md#repository-ports) (the port), never on `MongoRepository[T, ID]` (the adapter). MongoDB is the default document adapter today — but the layer is designed so any document backend (DynamoDB, Elasticsearch, etc.) can be added by implementing the same ports. See [Extending PyFly Data](data.md#extending-pyfly-data).

PyFly Data Document provides a document-oriented data access layer that implements the same Repository pattern and derived query method convention as the relational adapter — backed by MongoDB via Beanie ODM on pymongo's async client, on the same unit-of-work model as the relational adapter: `@transactional`, repository calls outside a transaction, propagation, rollback rules and synchronizations behave the same on both.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Document Definition](#document-definition)
  - [BaseDocument](#basedocument)
  - [Audit Trail Fields](#audit-trail-fields)
  - [Aggregate Documents and Domain Events](#aggregate-documents-and-domain-events)
  - [Settings Class](#settings-class)
  - [Indexed Fields](#indexed-fields)
  - [Defining Your Own Documents](#defining-your-own-documents)
- [MongoRepository\[T, ID\]](#mongorepositoryt-id)
  - [Units of Work](#units-of-work)
  - [Creating a Repository](#creating-a-repository)
  - [CRUD Methods Reference](#crud-methods-reference)
- [Derived Query Methods](#derived-query-methods)
  - [How It Works](#how-it-works)
  - [MongoQueryMethodCompiler Operator Mapping](#mongoquerymethodcompiler-operator-mapping)
  - [Connectors](#connectors)
  - [Ordering](#ordering)
  - [Result Shapes](#result-shapes)
  - [Complete Derived Query Examples](#complete-derived-query-examples)
- [Custom Queries with @query](#custom-queries-with-query)
  - [Find Filter Queries](#find-filter-queries)
  - [Aggregation Pipelines](#aggregation-pipelines)
  - [Parameter Substitution](#parameter-substitution)
  - [MongoQueryExecutor Internals](#mongoqueryexecutor-internals)
- [Specifications and Filter Operators](#specifications-and-filter-operators)
- [Configuration](#configuration)
  - [DocumentProperties](#documentproperties)
  - [pyfly.yaml Keys](#pyflyyaml-keys)
  - [Environment Variables](#environment-variables)
- [Auto-Configuration](#auto-configuration)
  - [Detection Flow](#detection-flow)
  - [Beanie Initialization](#beanie-initialization)
  - [Document Class Discovery](#document-class-discovery)
  - [Health and Metrics](#health-and-metrics)
- [Transaction Management](#transaction-management)
  - [The unified @transactional decorator](#the-unified-transactional-decorator)
  - [Replica Set Requirement](#replica-set-requirement)
  - [Usage Example](#usage-example)
- [MongoRepositoryBeanPostProcessor](#mongorepositorybeanpostprocessor)
  - [How It Works](#how-it-works_1)
  - [Stub Detection](#stub-detection)
- [Pagination](#pagination)
  - [Paginated Queries](#paginated-queries)
  - [Sort Specification Building](#sort-specification-building)
- [Integration with Web Layer](#integration-with-web-layer)
  - [Controller with Valid\[T\] and MongoRepository](#controller-with-validt-and-mongorepository)
- [Complete CRUD Example](#complete-crud-example)
- [See Also](#see-also)

---

## Architecture Overview

All concrete types live in the MongoDB adapter package. The namespace `pyfly.data.document` is a pass-through and does not re-export anything.

```python
from pyfly.data.document.mongodb import (
    AggregateDocument,
    BaseDocument,
    MongoFilterOperator,
    MongoQueryMethodCompiler,
    MongoRepository,
    MongoRepositoryBeanPostProcessor,
    MongoSpecification,
    MongoTransactionManager,
    current_session,
)
```

> **Note:** Always import concrete types from `pyfly.data.document.mongodb`. Commons types (`Page`, `Pageable`, `RepositoryPort`, etc.) are imported from `pyfly.data` — see the [Data Module Guide](data.md#import-rules).

Source files:
- `src/pyfly/data/__init__.py` — commons layer (Page, Pageable, ports)
- `src/pyfly/data/document/mongodb/__init__.py` — MongoDB adapter package exports

---

## Document Definition

### BaseDocument

`BaseDocument` is the base class for all MongoDB documents in a PyFly application. It extends `beanie.Document` and provides audit trail fields that are automatically populated on insert and update.

```python
from pyfly.data.document.mongodb import BaseDocument
```

All domain documents should inherit from `BaseDocument` to gain the automatic audit trail, just as all relational entities inherit from `BaseEntity` in the SQLAlchemy adapter.

### Audit Trail Fields

`BaseDocument` provides four audit trail fields in addition to the `id` field inherited from `beanie.Document`:

| Field        | Type              | Set on                                    | Description                          |
|--------------|-------------------|-------------------------------------------|--------------------------------------|
| `id`         | `PydanticObjectId`| insert (made on the client)               | Document primary key (ObjectId)      |
| `created_at` | `datetime`        | insert                                    | When the document was created (aware UTC) |
| `updated_at` | `datetime`        | insert and every update                   | When the document last changed (aware UTC) |
| `created_by` | `str \| None`     | insert, unless the application set it     | Who created it                       |
| `updated_by` | `str \| None`     | insert and every update                   | Who changed it last                  |

They are kept current on every write path, as the relational `BaseEntity`'s are: Beanie event actions of `BaseDocument` stamp them with the application's `AuditorAware` (who) and `DateTimeProvider` (when), from `pyfly.data.auditing`:

- **Insert** (`insert()`, `save()` of a new document, `MongoRepository.save`/`save_all`): `created_at` and `updated_at` are the provider's time; `created_by` and `updated_by` the auditor, unless the application set them.
- **Update** (`save()` of a stored document, `replace()`, `update()`/`set()`, `save_changes()` when something changed): `updated_at` is the provider's time, and `updated_by` the current auditor (`None` when there is none: a job with no principal does not leave the last user's name on its change), unless the application changed it itself (known with state management).

The stamping runs while auditing is enabled (`pyfly.data.auditing.enabled`, on by default: the document auto-configuration registers a `DocumentAuditingHandler`). The default auditor is the authenticated user of the security context; a scheduled job, a listener or a shell command names its principal with `run_as("system")`. A bulk update through a query (`OrderDocument.find(...).update(...)`) sends one command and runs no document hooks, as a bulk `UPDATE` does on SQL: stamp `updated_at` and `updated_by` in it yourself (`pyfly.data.auditing.current_auditor()`). Before 26.09.08 the fields were never maintained: `updated_at` stayed at insert time and `created_by`/`updated_by` were never set.

Timestamps are aware UTC `datetime` values: a naive value is taken as UTC, an aware one converted to UTC, and a stamped time is cut to the millisecond BSON stores, so what a save returns equals what a load reads (the client is built with `tz_aware=True` for the same reason).

`BaseDocument` enables Beanie state management for the documents that declare no `Settings` of their own. A subclass's `Settings` class replaces `BaseDocument`'s (Beanie does not merge them): declare `use_state_management = True` in it to keep change tracking (`save_changes()`, the audit hooks' knowledge of what the application changed).

### Aggregate Documents and Domain Events

A Beanie document cannot inherit `pyfly.domain.AggregateRoot` (its slots conflict with pydantic's). `AggregateDocument` (a `BaseDocument`) gives it the same contract: `raise_event()`, `pending_events()` and `clear_events()`.

```python
from pyfly.data.document.mongodb import AggregateDocument


class OrderDocument(AggregateDocument):
    status: str = "NEW"

    class Settings:
        name = "orders"

    def confirm(self) -> None:
        self.status = "CONFIRMED"
        self.raise_event(OrderConfirmed(order_id=str(self.id)))
```

The application's `DomainEventPublisher` (`pyfly.eda.domain_events`) publishes them as the document's unit of work commits: an event raised inside a unit is tied to it, and the pending events of a document `MongoRepository.save`/`save_all` writes are tied to the unit of the save. A unit that rolls back publishes nothing, and the events stay pending on the document. Listeners declared with a transaction phase run at that phase (`AFTER_COMMIT`: once the unit committed).

### Settings Class

Every Beanie document uses an inner `Settings` class to configure collection-level options. The most important setting is `name`, which defines the MongoDB collection name:

```python
class UserDocument(BaseDocument):
    name: str
    email: str

    class Settings:
        name = "users"
```

If you omit the `Settings` class or the `name` attribute, Beanie derives the collection name from the class name (e.g., `UserDocument` becomes `UserDocument` as the collection name). It is best practice to always set `name` explicitly for clarity and consistency.

Common `Settings` attributes:

| Attribute              | Type   | Description                                           |
|------------------------|--------|-------------------------------------------------------|
| `name`                 | `str`  | MongoDB collection name                               |
| `use_state_management` | `bool` | Track field changes for partial updates (`BaseDocument`'s is replaced by a subclass's `Settings`: declare it again) |
| `use_revision`         | `bool` | Optimistic locking: a write of a stale copy raises `OptimisticLockingFailureException` |
| `indexes`              | `list` | Additional Beanie index definitions                   |

### Indexed Fields

You can define indexes using Beanie's `Indexed` type or the `Settings.indexes` list. The `Indexed` type is the simplest approach for single-field indexes:

```python
from beanie import Indexed

class UserDocument(BaseDocument):
    name: str
    email: Indexed(str, unique=True)
    role: Indexed(str)

    class Settings:
        name = "users"
```

For compound indexes or more complex index configurations, use the `Settings.indexes` list:

```python
from pymongo import IndexModel, ASCENDING, DESCENDING

class OrderDocument(BaseDocument):
    customer_id: str
    status: str
    total: float
    region: str

    class Settings:
        name = "orders"
        indexes = [
            IndexModel(
                [("customer_id", ASCENDING), ("status", ASCENDING)],
                name="idx_customer_status",
            ),
            IndexModel(
                [("region", ASCENDING), ("total", DESCENDING)],
                name="idx_region_total",
            ),
        ]
```

Indexes are created automatically when Beanie initializes the document models during application startup.

### Defining Your Own Documents

Extend `BaseDocument` and declare your domain fields using standard Pydantic field declarations:

```python
from pyfly.data.document.mongodb import BaseDocument
from beanie import Indexed
from pydantic import Field


class ProductDocument(BaseDocument):
    name: str
    sku: Indexed(str, unique=True)
    description: str = ""
    price: float = Field(gt=0)
    category: str
    tags: list[str] = Field(default_factory=list)
    active: bool = True

    class Settings:
        name = "products"
```

This document will have all five inherited fields (`id`, `created_at`, `updated_at`, `created_by`, `updated_by`) plus your seven custom fields. Because `BaseDocument` extends `beanie.Document` (which extends `pydantic.BaseModel`), all Pydantic validation, serialization, and field configuration features are available.

Nested documents use standard Pydantic models:

```python
from pydantic import BaseModel


class Address(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str


class CustomerDocument(BaseDocument):
    name: str
    email: str
    address: Address | None = None
    tags: list[str] = Field(default_factory=list)

    class Settings:
        name = "customers"
```

Source file: `src/pyfly/data/document/mongodb/document.py`

---

## MongoRepository[T, ID]

The `MongoRepository[T, ID]` class provides generic async CRUD operations for Beanie documents. It mirrors the `Repository[T, ID]` class of the SQLAlchemy adapter, runs on the same unit-of-work model, and satisfies the `BatchRepository` port (`CrudRepository` → `ReactiveSortingRepository` → `PagingAndSortingRepository` → `BatchRepository`). The two type parameters are:

- **T** — the document type (any Beanie `Document`; `BaseDocument` adds the audit fields)
- **ID** — the id type callers pass (`str` works for an `ObjectId` document: ids are converted, see below)

When you subclass `MongoRepository[T, ID]` with concrete type parameters, the framework extracts the document type and ID type in `__init_subclass__` (through intermediate generic bases too). No explicit `__init__` is needed:

```python
from pyfly.data.document.mongodb import MongoRepository, BaseDocument
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class ProductRepository(MongoRepository[ProductDocument, str]):
    pass

# Usage:
product = await repo.save(ProductDocument(name="Widget", price=9.99, category="gadgets"))
found = await repo.find_by_id(str(product.id))
```

`MongoRepository[T, ID]` satisfies the [`RepositoryPort[T, ID]`](data.md#repository-ports) protocol, enabling hexagonal architecture where your service layer depends on the port, not the adapter.

### Units of Work

A `MongoRepository` never holds a session. Every public `async def` (on `MongoRepository`, on your subclass, and the derived and `@query` methods the post-processor compiles) runs in an **operation scope**, exactly as the relational repository's calls do:

- **Inside a unit of work** for the repository's datasource (`@transactional`, a `TransactionTemplate` block, a message listener delivery), the call joins it: every Beanie and pymongo operation gets the unit's `ClientSession` (`session=`), terminal ones included (`to_list()`, counts, deletes, updates, aggregations, bulk writes), so the writes are part of the transaction. Operations on the session run under the unit's operation guard: `asyncio.gather` fan-out inside a transaction is serialized instead of interleaving commands on one session.
- **Outside a transaction**, the outermost repository call opens a short **auto unit** of its own:
  - a *read* method (`find*`, `count*`, `exists*`, `stream*`, `get*`, `scroll*`, by the relational naming rule: `find_or_create` and `find_and_modify` write) runs in a session without a transaction (one round trip), retried once when its connection turns out to be dead;
  - any other method runs in a transaction that commits at the end of the call, so `save_all`, or a subclass method that writes twice, is atomic;
  - a method that sends one command runs without a transaction: `save`, `delete`, `delete_by_id`, the bulk deletes and derived `delete_by_*` when the document class has no delete event actions. MongoDB runs one command atomically on each document it touches;
  - on a standalone server (no transactions) every auto unit runs without one.

The datasource is the one named by `datasource=` (constructor) or the class attribute `__datasource__`; without one it is the datasource of the transaction manager that serves the client the document class is bound to (`"document"`, `pyfly.data.document.datasource`). Custom methods reach the current session with `self._session`:

```python
@repo_stereotype
class OrderRepository(MongoRepository[OrderDocument, str]):
    async def close_stale(self, before: datetime) -> int:
        result = await OrderDocument.get_pymongo_collection().update_many(
            {"status": "OPEN", "updated_at": {"$lt": before}}, {"$set": {"status": "CLOSED"}}, session=self._session
        )
        return result.modified_count
```

A persistence failure is raised translated to the kernel's exceptions, from the driver's (`pyfly.data.document.mongodb.exception_translation`): a duplicate key (alone or in a bulk write) is a `DuplicateKeyException` naming the unique index, a document validation failure a `DataIntegrityException`, a Beanie revision conflict an `OptimisticLockingFailureException`, and a write conflict (or any other transient transaction error) a `ConcurrencyException`, which a retry can get past.

### Creating a Repository

Subclass `MongoRepository[T, ID]` with concrete type parameters and register it with the `@repository` stereotype:

```python
from pyfly.data.document.mongodb import MongoRepository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class ProductRepository(MongoRepository[ProductDocument, str]):
    __sortable__ = ("name", "price")      # the only fields a Sort may name (default: every field)
    __filterable__ = ("category", "active")  # the only fields find_all(**filters) may filter on
```

For documents with `PydanticObjectId` primary keys (the default):

```python
from beanie import PydanticObjectId


@repo_stereotype
class OrderRepository(MongoRepository[OrderDocument, PydanticObjectId]):
    pass
```

### CRUD Methods Reference

| Method                                     | Return Type         | Description                                    |
|--------------------------------------------|---------------------|------------------------------------------------|
| `save(entity)`                             | `T`                 | Insert a new document, or upsert one that is not new (one command) |
| `save_all(entities)`                       | `list[T]`           | One ordered bulk write for new and existing documents |
| `find_by_id(id: ID)`                       | `T \| None`         | Find by id (converted to the document's id type) |
| `find_all_by_id(ids)`                      | `list[T]`           | Find all documents whose id is in `ids`        |
| `find_all(**filters)`                      | `list[T]`           | Find all, optionally filtered by field equality |
| `find_all(sort: Sort)`                     | `list[T]`           | Fetch all documents sorted by `sort`           |
| `find_all(pageable: Pageable)`             | `Page[T]`           | Paginated query with the `Pageable`'s sort     |
| `find_slice(pageable, **filters)`          | `Slice[T]`          | A page and whether another follows, with no count |
| `stream_all(sort: Sort \| None = None, **filters)` | `AsyncIterator[T]` | Stream documents (Flux analogue) through one cursor |
| `count()`                                  | `int`               | Count all documents in the collection          |
| `exists_by_id(id: ID)`                     | `bool`              | Whether a document with this id exists (`{_id: 1}` of at most one document) |
| `delete(entity: T)`                        | `None`              | Delete the given document instance (its delete event actions run) |
| `delete_by_id(id: ID)`                     | `None`              | Delete by id (no-op if not found)              |
| `delete_all(entities=None)`                | `None`              | Delete the given documents, or all if `None`   |
| `delete_all_by_id(ids)`                    | `None`              | Delete all documents whose id is in `ids`      |
| `delete_all_in_batch(entities=None)`       | `None`              | Bulk delete, bypassing delete event actions    |
| `delete_all_by_id_in_batch(ids)`           | `None`              | Bulk delete by ids, bypassing delete event actions |
| `find_all_by_spec(spec)`                   | `list[T]`           | Documents matching a `MongoSpecification`      |
| `find_all_by_spec_paged(spec, pageable)`   | `Page[T]`           | A page of them                                 |
| `find_slice_by_spec(spec, pageable)`       | `Slice[T]`          | A slice of them, with no count                 |

**save()** inserts a document that is new (its `is_new()` hook when the class defines one, else `id is None`) and upserts any other (Beanie's `save()`). A new document gets its id on the client when its id type allows: a `PydanticObjectId`, the hex string of an `ObjectId` for a `str` id, a random `UUID`, so a `str`-id document is stored with a string `_id` and found by it.

**save_all()** is one ordered `bulk_write`: `InsertOne` for the new documents and an `UpdateOne` (`$set` of the document, `$unset` of its `None` fields, upsert) for the others. It runs Beanie's validation (`validate_on_save`), the insert or save/update event actions, and state management for each document, as `save` does, so returned documents work with `save_changes()`. With `use_revision`, a stored document's update is guarded by its revision: a stale one raises `OptimisticLockingFailureException`, and in a transaction the unit is marked rollback-only.

**Ids** are converted to the document's id type in every `_id` filter (`find_by_id`, `find_all_by_id`, `exists_by_id`, the deletes, `find_all(id=...)`, derived queries on `id`): `MongoRepository[Doc, str]` finds an `ObjectId` document by its string. An id the type cannot hold (`"abc"` for an `ObjectId`) matches nothing.

**Names**: `find_all(**filters)` keys, `Sort` orders and derived query names are the document's Python fields. `id` is matched as `_id`, and an aliased field under its alias (`Field(alias="displayName")`). A name that is not a field of the document raises `InvalidPropertyError` (a 400), optionally narrowed by `__sortable__` and `__filterable__`; a dotted path into an embedded document keeps what follows its first segment as written.

```python
orders = await repo.find_all(status="PENDING", customer_id="abc")
# find({"status": "PENDING", "customer_id": "abc"}) in the call's session
```

**Sorting**: `Order.null_handling` and `Order.ignore_case` are honored. MongoDB puts nulls (and missing fields) first in ascending order and last in descending order; `nulls_last()` on an ascending order, `nulls_first()` on a descending one, and `ignoring_case()` (lower-cased strings) sort on computed keys, in one aggregation instead of a `find`. Paging orders by `_id` after the requested orders, so every page is deterministic.

**Deletes**: `delete_by_id`, `delete_all_by_id`, `delete_all()` and `delete_all(entities)` send one `delete_one`/`delete_many` when the document class has no delete event actions (`@before_event(Delete)`, `@after_event(Delete)`); with them, the documents are loaded and deleted one by one so the actions run. The `*_in_batch` forms always delete in bulk (Spring's `deleteAllInBatch`).

**stream_all()** is the Flux<T> analogue: one cursor on the unit's session (inside a transaction), or on a read unit of its own that it keeps until the iterator is exhausted or closed:

```python
async for product in repo.stream_all(Sort.by("name")):
    process(product)
```

**find_all(pageable)** reads the page (sorted, `skip`/`limit`) and counts only when the page does not prove the total (a first page shorter than its size needs no count); `find_slice` never counts.

Source file: `src/pyfly/data/document/mongodb/repository.py`

---

## Derived Query Methods

PyFly automatically generates MongoDB query implementations from method names using the same Spring Data naming convention as the relational adapter. You define stub methods on your repository, and the `MongoRepositoryBeanPostProcessor` compiles them into real MongoDB queries at startup.

For the full naming convention reference (prefixes, operators, connectors, ordering), see the [Data Module Guide — Derived Query Methods](data.md#derived-query-methods).

### How It Works

The derived query pipeline consists of two stages, split between the shared commons layer and the MongoDB adapter:

1. **Parsing (shared):** the post-processor parses the method name with the shared `QueryMethodParser` **against the document's fields** (`parse(name, properties=...)`), so a name that names no field fails when the repository is built (`InvalidQueryMethodError`) instead of matching nothing, or, for `delete_by_*`, everything. Parsed against the fields, the full keyword set applies (`_is`, `_equals`, `_after`, `_before`, `_true`, `_false`, `_not_in`, `_not_like`, `_starting_with`, `_ending_with`, `_not_containing`, `_ignore_case`, `_all_ignore_case`).

2. **Compilation (adapter-specific):** `MongoQueryMethodCompiler.compile()` turns the `ParsedQuery` into a `MongoDerivedQuery`, which builds the filter from the call's arguments (each field mapped to its stored name, `id` to `_id` with the argument converted to the document's id type) and runs it through the repository, in its unit of work.

### MongoQueryMethodCompiler Operator Mapping

The operators mean what they mean on SQL (`pyfly.data.document.mongodb.criteria`), so a derived query returns the same documents a relational repository returns rows:

| Operator       | Method Suffix          | MongoDB Filter Expression                    | Args |
|----------------|------------------------|----------------------------------------------|------|
| `eq`           | *(none)*, `_is`, `_equals` | `{field: value}`                         | 1    |
| `not`          | `_not`, `_is_not`      | `{field: {"$nin": [value, None]}}`           | 1    |
| `gt`           | `_greater_than`, `_after` | `{field: {"$gt": value}}`                 | 1    |
| `gte`          | `_greater_than_equal`  | `{field: {"$gte": value}}`                   | 1    |
| `lt`           | `_less_than`, `_before` | `{field: {"$lt": value}}`                   | 1    |
| `lte`          | `_less_than_equal`     | `{field: {"$lte": value}}`                   | 1    |
| `between`      | `_between`             | `{field: {"$gte": low, "$lte": high}}`       | 2    |
| `like`         | `_like`                | `{field: {"$regex": "^...", "$options": "s"}}` (anchored) | 1 |
| `not_like`     | `_not_like`            | `{field: {"$not": <like>, "$ne": None}}`     | 1    |
| `containing`   | `_containing`          | `{field: {"$regex": "<escaped value>", "$options": "s"}}` | 1 |
| `starting_with` | `_starting_with`      | `{field: {"$regex": "^<escaped value>"}}`    | 1    |
| `ending_with`  | `_ending_with`         | `{field: {"$regex": "<escaped value>\\z"}}`  | 1    |
| `in`           | `_in`                  | `{field: {"$in": values}}`                   | 1 (list) |
| `not_in`       | `_not_in`              | `{field: {"$nin": [*values, None]}}`         | 1 (list) |
| `is_null`      | `_is_null`, `_null`    | `{field: None}` (null or missing)            | 0    |
| `is_not_null`  | `_is_not_null`, `_not_null` | `{field: {"$ne": None}}`                | 0    |
| `is_true` / `is_false` | `_true` / `_false` | `{field: True}` / `{field: False}`       | 0    |

Key notes on the MongoDB-specific behavior:

- **`like`** follows SQL `LIKE`: the pattern is anchored at both ends and case-sensitive; `%` matches any run of characters (line breaks included), `_` one character, and every other character is itself (regex-escaped). `find_by_code_like("INV-%")` matches `INV-001`, never `XINV-002`, and its regex starts with `^`, so MongoDB can use an index on the field (a bounded index scan from `"INV-"`); an unanchored or case-insensitive regex cannot.
- **`containing`**, **`starting_with`** and **`ending_with`** match their argument as it is (its `%` and `_` are plain characters), case-sensitive. `_ignore_case` after a predicate (or `_all_ignore_case` at the end of the criteria) makes `eq`, `not` and the pattern operators case-insensitive (`$options: "i"`). Before 26.09.08, derived `_containing` ignored case and `_like` was unanchored.
- **Negation** follows SQL's three-valued logic: `_not`, `_not_in` and `_not_like` are false for a document whose field is null or missing, as `NULL <> 'x'` is not true on SQL. MongoDB's own `$ne`/`$nin` would match those documents. `_in` with an empty list matches nothing and `_not_in` with an empty list matches everything, as on SQL.
- **`between`** consumes two arguments and combines them into a single filter with both `$gte` and `$lte`.
- **`is_null`**, **`is_not_null`**, **`is_true`** and **`is_false`** consume zero arguments.

### Connectors

Multiple predicates are connected with `_and_` or `_or_`. `and` binds tighter than `or`, as in Spring and in SQL: the shared parser returns the predicates as an `or` of `and` groups (`ParsedQuery.groups`), and the compiler builds an `$or` of `$and` groups:

```python
# AND: {"$and": [{"status": "ACTIVE"}, {"role": "admin"}]}
async def find_by_status_and_role(self, status: str, role: str) -> list[UserDocument]: ...

# OR: {"$or": [{"status": "ACTIVE"}, {"role": "admin"}]}
async def find_by_status_or_role(self, status: str, role: str) -> list[UserDocument]: ...

# Mixed: and binds tighter, so this is (status = ? AND role = ?) OR active = ?
# -> {"$or": [{"$and": [{"status": ?}, {"role": ?}]}, {"active": ?}]}
async def find_by_status_and_role_or_active(
    self, status: str, role: str, active: bool
) -> list[UserDocument]: ...
```

For a single predicate with no connectors, the filter is a plain document (no `$and` wrapper):

```python
# Simple: {"email": value}
async def find_by_email(self, email: str) -> list[UserDocument]: ...
```

### Ordering

Append `_order_by_{field}_{asc|desc}` to control result ordering. The name's orders come first, then those of a `Sort` or `Pageable` parameter:

```python
# Sort by created_at descending
async def find_by_status_order_by_created_at_desc(
    self, status: str
) -> list[OrderDocument]: ...

# Multiple sort fields
async def find_by_active_order_by_name_asc_created_at_desc(
    self, active: bool
) -> list[UserDocument]: ...
```

### Result Shapes

The return annotation decides what a `find_by_` method returns (the shared `result_shape`):

| Annotation                 | Result                                                                  |
|----------------------------|-------------------------------------------------------------------------|
| `list[T]`                  | Every matching document                                                  |
| `T \| None` (or `T`)       | The one matching document, or `None`; more than one raises `IncorrectResultSizeException` |
| `Page[T]` (with a `Pageable` parameter) | A page and the total                                        |
| `Slice[T]` (with a `Pageable` parameter) | A page and whether another follows, with no count          |
| `list[Projection]`         | Objects with the projection's fields, read with a server-side projection |
| `list[dict]`               | Raw documents                                                            |

A `find_by_` method that returns several documents may take a `Sort` or a `Pageable` parameter (they bind no value). A scalar, a tuple row or another class is refused at startup. `count_by_` returns an `int` (`countDocuments`), `exists_by_` a `bool` (it reads `{_id: 1}` of at most one document), and `delete_by_` the number of documents deleted (one `deleteMany`, or document by document when the class has delete event actions).

### Complete Derived Query Examples

```python
@repo_stereotype
class OrderRepository(MongoRepository[OrderDocument, PydanticObjectId]):

    # Equals (default operator)
    # -> {"status": value}
    async def find_by_status(self, status: str) -> list[OrderDocument]: ...

    # One result or None (more than one raises IncorrectResultSizeException)
    async def find_by_reference(self, reference: str) -> OrderDocument | None: ...

    # Multiple conditions with AND
    # -> {"$and": [{"customer_id": value}, {"status": value}]}
    async def find_by_customer_id_and_status(
        self, customer_id: str, status: str
    ) -> list[OrderDocument]: ...

    # Greater than
    # -> {"total": {"$gt": value}}
    async def find_by_total_greater_than(self, min_total: float) -> list[OrderDocument]: ...

    # Between (takes 2 arguments)
    # -> {"total": {"$gte": low, "$lte": high}}
    async def find_by_total_between(self, low: float, high: float) -> list[OrderDocument]: ...

    # Contains, case-sensitive, the fragment matched as it is
    # -> {"customer_id": {"$regex": "<escaped fragment>", "$options": "s"}}
    async def find_by_customer_id_containing(self, fragment: str) -> list[OrderDocument]: ...

    # Contains, whatever the case
    async def find_by_customer_id_containing_ignore_case(self, fragment: str) -> list[OrderDocument]: ...

    # IN a list
    # -> {"status": {"$in": ["PENDING", "SHIPPED"]}}
    async def find_by_status_in(self, statuses: list[str]) -> list[OrderDocument]: ...

    # Not equal: false for a missing or null status, as on SQL
    # -> {"status": {"$nin": ["CANCELLED", None]}}
    async def find_by_status_not(self, status: str) -> list[OrderDocument]: ...

    # IS NULL (zero arguments consumed)
    # -> {"deleted_at": None}
    async def find_by_deleted_at_is_null(self) -> list[OrderDocument]: ...

    # COUNT prefix
    # -> countDocuments({role: value})
    async def count_by_role(self, role: str) -> int: ...

    # EXISTS prefix
    # -> find_one({email: value}, {_id: 1})
    async def exists_by_email(self, email: str) -> bool: ...

    # DELETE prefix (returns number of deleted documents)
    # -> deleteMany({status: value})
    async def delete_by_status(self, status: str) -> int: ...

    # Paged, with the name's order first
    async def find_by_status_order_by_created_at_desc(
        self, status: str, pageable: Pageable
    ) -> Page[OrderDocument]: ...

    # Complex: AND + ordering
    async def find_by_status_and_customer_id_order_by_total_desc(
        self, status: str, customer_id: str
    ) -> list[OrderDocument]: ...
```

Each method body should be a stub (`...` or `pass`). The `MongoRepositoryBeanPostProcessor` detects them and replaces them with real implementations at startup.

Source file: `src/pyfly/data/document/mongodb/query_compiler.py`

---

## Custom Queries with @query

For queries that cannot be expressed through method naming conventions, the `@query` decorator lets you write MongoDB filter documents or aggregation pipelines directly as JSON strings with named parameter substitution.

```python
from pyfly.data.query import query  # backend-neutral @query decorator
```

The `@query` decorator is backend-neutral and lives in `pyfly.data.query`. It is shared between the relational and document adapters. For MongoDB, the `MongoQueryExecutor` compiles the decorated methods into async callables that execute against Beanie document models.

### Find Filter Queries

A query string that starts with `{` is treated as a MongoDB **find filter**:

```python
@repo_stereotype
class UserRepository(MongoRepository[UserDocument, str]):

    @query('{"email": ":email"}')
    async def find_by_email_exact(self, email: str) -> list[UserDocument]: ...

    @query('{"active": true, "role": ":role"}')
    async def find_active_by_role(self, role: str) -> list[UserDocument]: ...

    @query('{"age": {"$gte": ":min_age", "$lte": ":max_age"}}')
    async def find_by_age_range(self, min_age: int, max_age: int) -> list[UserDocument]: ...
```

The compiled query runs `find` in the call's unit of work (its session) and returns `list[entity]`, through Beanie, so the documents come back with their state saved.

### Aggregation Pipelines

A query string that starts with `[` is treated as a MongoDB **aggregation pipeline**:

```python
@repo_stereotype
class OrderRepository(MongoRepository[OrderDocument, str]):

    @query('[{"$match": {"status": ":status"}}, {"$group": {"_id": "$category", "total": {"$sum": "$amount"}}}]')
    async def total_by_category(self, status: str) -> list[dict]: ...

    @query('[{"$match": {"customer_id": ":customer_id"}}, {"$sort": {"created_at": -1}}, {"$limit": 10}]')
    async def recent_orders(self, customer_id: str) -> list[dict]: ...
```

Aggregation pipeline queries use the underlying pymongo collection directly (via `get_pymongo_collection()`, with the unit's session) and return `list[dict]` rather than document instances. A find filter and a pipeline are reads (outside a transaction they run without one). A pipeline with an `$out` or `$merge` stage is a write of one command, and MongoDB refuses both stages inside a multi-document transaction: outside a transaction the call runs without one, and inside `@transactional` it raises `IllegalTransactionStateError` before anything is sent (the transaction stays usable). Run such a query outside the transaction, or in a boundary with `Propagation.NOT_SUPPORTED`.

### Parameter Substitution

Named parameters use the `:param_name` convention inside JSON string values. During execution, the `MongoQueryExecutor` substitutes parameter values while preserving Python types:

| Placeholder in JSON | Method Parameter | Substituted Value |
|---|---|---|
| `":email"` (exact match) | `email="alice@example.com"` | `"alice@example.com"` (str) |
| `":min_age"` (exact match) | `min_age=18` | `18` (int, not string) |
| `":active"` (exact match) | `active=True` | `True` (bool) |
| `"prefix_:name_suffix"` (embedded) | `name="alice"` | `"prefix_alice_suffix"` (str interpolation) |

**Substitution rules:**

- If the entire JSON string value is a single `:param_name` placeholder, it is replaced by the actual Python value, preserving the type (int, bool, list, etc.).
- If `:param_name` appears within a larger string, it is replaced via string interpolation with `str(value)`.
- Dicts and lists are recursed into.
- Non-string values (int, float, bool, None) pass through unchanged.

```python
# The filter {"age": ":min_age"} with min_age=18
# becomes {"age": 18}  (int, not "18")

# The filter {"name": {"$regex": ".*:pattern.*"}} with pattern="alice"
# becomes {"name": {"$regex": ".*alice.*"}}  (string interpolation)
```

### MongoQueryExecutor Internals

The `MongoQueryExecutor` is used by the `MongoRepositoryBeanPostProcessor` to compile `@query`-decorated methods at startup:

1. **Validation:** Checks that the method has a `__pyfly_query__` attribute (set by the `@query` decorator).
2. **JSON parsing:** Parses the query string once at compile time to validate it and detect whether it is a find filter (JSON object) or an aggregation pipeline (JSON array).
3. **Template compilation:** Stores the parsed template in a `MongoAnnotatedQuery`, which substitutes the parameters at execution time and runs the query in the repository's unit of work.

```python
from pyfly.data.document.mongodb.query import MongoQueryExecutor

executor = MongoQueryExecutor()
compiled = executor.compile_query_method(method, entity_type)   # a MongoAnnotatedQuery
await compiled.run(repository, email="alice@example.com")        # -> list[UserDocument], in the repository's unit
await compiled(UserDocument, email="alice@example.com")          # through a repository of the class
```

**Source:** `src/pyfly/data/document/mongodb/query.py`

---

## Specifications and Filter Operators

`MongoSpecification` composes filter documents with `&` (`$and`), `|` (`$or`) and `~`; `MongoFilterOperator` builds them field by field, and `MongoFilterUtils` from keyword arguments, dicts and examples (a pydantic example by its fields). They mean what the relational ones mean:

- Field names are the document's Python fields, resolved when the specification is applied: `id` is matched as `_id` (an id is converted to the document's id type) and an aliased field under its alias. A name that is not a field raises `InvalidPropertyError`; a dotted path into an embedded document keeps what follows its first segment as written.
- `like(field, pattern)` is SQL `LIKE`: anchored and case-sensitive (pass `ignore_case=True` to ignore case); `contains(field, value)` matches the value as it is (case-sensitive unless `ignore_case=True`). Before 26.09.08, `like` ignored case.
- `neq(field, value)` is false for a null or missing field, as `!=` is on SQL, and `~spec` follows SQL's three-valued logic: it matches the documents for which `spec` is false, never those for which it is unknown (`NOT (role = 'admin')` leaves out a document without a role). MongoDB's own `$ne` and `$nor` would match them. `~` of a specification that filters nothing still filters nothing.

```python
from pyfly.data.document.mongodb import MongoFilterOperator as F

active_admins = F.eq("role", "admin") & F.eq("active", True)
not_admins = ~F.eq("role", "admin")                 # role present and not 'admin'
by_prefix = F.like("code", "INV-%")                 # anchored: can use an index on code
page = await repo.find_all_by_spec_paged(active_admins | by_prefix, Pageable.of(1, 20))
```

Source files: `src/pyfly/data/document/mongodb/specification.py`, `filter.py`, `criteria.py`

---

## Configuration

### DocumentProperties

The `DocumentProperties` dataclass (`pyfly.config.properties.mongodb`) captures the document database configuration under the `pyfly.data.document.*` namespace. Keys bind relaxed (`max-pool-size` and `max_pool_size` are the same key), and `DocumentProperties.client_options()` is what the auto-configured `AsyncMongoClient` is built with: the timeouts are in seconds here and converted to pymongo's milliseconds, and the `options` map is passed to the client as it is, winning over the rest.

| Field                      | Type          | Default                        | Description |
|----------------------------|---------------|--------------------------------|-------------|
| `enabled`                  | `bool`        | `False`                        | Enable the MongoDB subsystem |
| `uri`                      | `str`         | `"mongodb://localhost:27017"`  | MongoDB connection URI |
| `database`                 | `str`         | `"pyfly"`                      | Database name |
| `datasource`               | `str`         | `"document"`                   | The datasource name of the document units of work (`@transactional(datasource=...)`) |
| `min_pool_size`            | `int`         | `0`                            | Minimum connections in the pool (`minPoolSize`) |
| `max_pool_size`            | `int`         | `100`                          | Maximum connections in the pool (`maxPoolSize`) |
| `max_idle_time`            | `float \| None` | `None`                       | Seconds a pooled connection may stay idle (`maxIdleTimeMS`) |
| `connect_timeout`          | `float \| None` | `None` (pymongo: 20 s)       | Seconds to open a connection (`connectTimeoutMS`) |
| `server_selection_timeout` | `float \| None` | `None` (pymongo: 30 s)       | Seconds to find a server for an operation (`serverSelectionTimeoutMS`) |
| `socket_timeout`           | `float \| None` | `None`                       | Seconds a socket read or write may take (`socketTimeoutMS`) |
| `wait_queue_timeout`       | `float \| None` | `None`                       | Seconds an operation waits for a pooled connection (`waitQueueTimeoutMS`) |
| `app_name`                 | `str \| None` | `None`                         | The application name the server logs (`appname`) |
| `tz_aware`                 | `bool`        | `True`                         | Datetimes come back as aware UTC values |
| `uuid_representation`      | `str`         | `"standard"`                   | How UUIDs are stored (`standard` is BSON binary subtype 4, what other drivers read) |
| `options`                  | `dict`        | `{}`                           | Any other `AsyncMongoClient` keyword argument (`retryWrites`, `readPreference`...) |
| `models`                   | `list[str]`   | `[]`                           | Document classes, or modules and packages to scan for them, by dotted name |
| `transaction.read_concern` | `str \| None` | `None` (the client's)          | Read concern of the transactions (`snapshot`, `majority`...) |
| `transaction.write_concern`| `str \| None` | `None` (the client's)          | Write concern of the transactions (`majority`, a number of nodes) |
| `transaction.max_commit_time` | `float \| None` | `None`                    | Seconds a commit may take on the server (`maxCommitTimeMS`); a unit's `timeout=` wins |
| `transaction.default`      | `bool \| None` | `None`                        | Whether the document datasource is the default of `@transactional`; by default it is when the relational layer is not enabled |
| `health.timeout`           | `float`       | `2.0`                          | Seconds the readiness check waits for `ping` |

Before 26.09.08 the client was built from the URI alone: the documented pool settings were ignored, and `tz_aware=False` made every `BaseDocument` timestamp naive after a save and a load.

### pyfly.yaml Keys

Configure MongoDB in your `pyfly.yaml` (or `application.yml`):

```yaml
pyfly:
  data:
    document:
      enabled: true
      uri: mongodb://localhost:27017/?replicaSet=rs0
      database: my_app
      min_pool_size: 5
      max_pool_size: 50
      server-selection-timeout: 5
      app-name: orders-service
      transaction:
        read-concern: snapshot
        write-concern: majority
      options:
        retryWrites: true
```

For a MongoDB Atlas connection:

```yaml
pyfly:
  data:
    document:
      enabled: true
      uri: mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority
      database: production_db
      min_pool_size: 10
      max_pool_size: 100
```

For a replica set deployment (required for transactions):

```yaml
pyfly:
  data:
    document:
      enabled: true
      uri: mongodb://mongo1:27017,mongo2:27017,mongo3:27017/?replicaSet=rs0
      database: my_app
      min_pool_size: 5
      max_pool_size: 50
```

### Environment Variables

Following PyFly's configuration resolution order, you can override any MongoDB property with environment variables. The pattern is the YAML key path with dots replaced by underscores and uppercased:

| Environment Variable       | Overrides                   | Example                          |
|----------------------------|-----------------------------|----------------------------------|
| `PYFLY_DATA_DOCUMENT_ENABLED`   | `pyfly.data.document.enabled`    | `true`                           |
| `PYFLY_DATA_DOCUMENT_URI`       | `pyfly.data.document.uri`        | `mongodb://prod-host:27017`      |
| `PYFLY_DATA_DOCUMENT_DATABASE`  | `pyfly.data.document.database`   | `production_db`                  |
| `PYFLY_DATA_DOCUMENT_MIN_POOL_SIZE` | `pyfly.data.document.min_pool_size` | `10`                       |
| `PYFLY_DATA_DOCUMENT_MAX_POOL_SIZE` | `pyfly.data.document.max_pool_size` | `200`                      |
| `PYFLY_DATA_DOCUMENT_MODELS`    | `pyfly.data.document.models`     | `orders.documents,billing.documents` |

This is useful for containerized deployments where secrets and connection strings are injected via environment:

```bash
export PYFLY_DATA_DOCUMENT_ENABLED=true
export PYFLY_DATA_DOCUMENT_URI="mongodb+srv://user:secret@cluster.mongodb.net"
export PYFLY_DATA_DOCUMENT_DATABASE=production_db
```

Source file: `src/pyfly/config/properties/mongodb.py` (class `DocumentProperties`)

---

## Auto-Configuration

PyFly uses a decentralized, config-driven auto-configuration system to detect and wire the MongoDB adapter at startup. Each subsystem provides its own `@auto_configuration` class that is discovered via `pyfly.auto_configuration` entry points. This mirrors the Spring Boot auto-configuration pattern.

### Detection Flow

`DocumentAutoConfiguration` (in `src/pyfly/data/document/auto_configuration.py`) is active whenever Beanie is installed (`@conditional_on_class("beanie")`). It always registers the **`MongoRepositoryWiringCheck`**, which fails the start on a MongoDB repository whose derived or `@query` stubs no post-processor compiled: without the document layer enabled they would answer `None` without touching the database (the relational `RepositoryWiringCheck` does the same for relational repositories).

With `pyfly.data.document.enabled: true` it registers:

| Bean | Type | What it does |
|------|------|--------------|
| `mongo_client` | `AsyncMongoClient` | The client, from `DocumentProperties` (above), with the command and pool metrics listener. Contexts in one process that configure the same client share it |
| `odm_initializer` | `BeanieInitializer` | Binds the document classes to the database when the context starts |
| `mongo_post_processor` | `MongoRepositoryBeanPostProcessor` | Compiles derived and `@query` methods and binds every repository to the context's transaction managers |
| `mongo_transaction_manager` | `MongoTransactionManager` | The document datasource's transaction manager (`pyfly.data.document.transaction.*`) |
| `mongo_transaction_manager_registration` | lifecycle bean | Registers the manager in the context's `TransactionManagerRegistry` while the context runs (the default datasource when `transaction.default` says so) |
| `mongo_health_indicator` | `MongoHealthIndicator` | The readiness check of the datasource |
| `document_auditing_handler` | `DocumentAuditingHandler` | Stamps `BaseDocument` writes (`pyfly.data.auditing.enabled`, on by default) |

The client bean carries `@conditional_on_missing_bean(AsyncMongoClient, singletons_only=True)`: declare your own singleton `AsyncMongoClient` bean (TLS, a credentials callback, read preferences) and the auto-configured one backs off; the `BeanieInitializer`, the transaction manager and the health indicator then use yours, and the client is closed when the context disposes its resources. Until 26.09.07 the framework's client silently shadowed it. A request- or refresh-scoped `AsyncMongoClient` bean is a second client: the auto-configured one stays, as the `@primary` candidate that an injection by type receives.

### Beanie Initialization

Beanie requires explicit initialization before any document operations can be performed. The `BeanieInitializer` lifecycle bean (in `src/pyfly/data/document/mongodb/initializer.py`) handles this automatically. It starts in an early lifecycle phase, before the application's lifecycle beans (which may write documents when they start), and gives its binding and its client back as the context disposes its resources, after every bean that could still write stopped, so a final flush to MongoDB in a `@pre_destroy` or a lifecycle `stop()` still has its client.

Beanie binds a document class to one database for the whole process. A second application context that bound the same classes to another client or database would take them away from the first one, and stopping it would close the client the first one still uses. The initializer therefore records each binding in a process-level registry, reference counted:

- a context that binds a class to the client and database it is bound to already shares the binding (and the client: contexts that configure the same client share one);
- a context that would bind it to another client or database fails to start with `DocumentBindingError` (**one document datasource per process**);
- the client is closed when the last context that uses it disposes it.

### Document Class Discovery

`BeanieInitializer` finds the classes to bind without a list to maintain:

1. every `MongoRepository` bean class's document (any Beanie `Document`, not only `BaseDocument` subclasses);
2. every Beanie document class registered in the container;
3. `pyfly.data.document.models`: document classes, or modules and packages whose document classes are all taken, by dotted name;
4. recursively, the documents the `Link`/`BackLink` fields of those documents name, so a linked document without a repository of its own is bound too.

Before 26.09.08 only `BaseDocument` subclasses were initialized: a repository over another Beanie document, or a link target without a repository, failed at first use with `CollectionWasNotInitialized`.

### Health and Metrics

`MongoHealthIndicator` belongs to the readiness probe only (a MongoDB blip takes the pod out of the load balancer instead of restarting every replica): it answers `UP` when the server answers `ping` within `pyfly.data.document.health.timeout` (2 s), whatever the client's server selection timeout is, and its details name the datasource, the database and whether the server runs transactions.

The client's listener feeds the metrics registry, labeled `datasource`:

| Metric | Meaning |
|--------|---------|
| `pyfly_mongo_commands_total` | Commands sent, by `command` (a bounded set of names) and `outcome` (`success`/`failure`) |
| `pyfly_mongo_command_duration_seconds` | Histogram: how long each command took, by `command` |
| `pyfly_mongo_pool_checked_out` | Connections in use |
| `pyfly_mongo_pool_open` | Connections open (idle or in use) |
| `pyfly_mongo_pool_acquire_seconds` | Histogram: how long a checkout waited for a connection |
| `pyfly_mongo_pool_checkout_failures_total` | Checkouts that failed (a pool timeout, a closed pool...) |

Source files:
- `src/pyfly/data/document/auto_configuration.py` — `DocumentAutoConfiguration`, `MongoRepositoryWiringCheck`
- `src/pyfly/data/document/mongodb/initializer.py` — `BeanieInitializer` and the process-level bindings
- `src/pyfly/data/document/mongodb/health.py` — `MongoHealthIndicator`, `MongoMetrics`

---

## Transaction Management

### The unified `@transactional` decorator

MongoDB uses the **same** `@transactional` annotation as the relational adapter, with the same Spring semantics, on the same unit-of-work model. The `MongoTransactionManager` (`pyfly.data.document.mongodb.transaction_manager`) runs its units: a unit's resource is a pymongo `ClientSession` bound to the running task, and every repository call inside the unit passes it to the driver, so the writes commit or roll back together.

```python
from pyfly.container import service
from pyfly.data import transactional


@service
class AccountService:
    def __init__(self, accounts: AccountRepository) -> None:
        self._accounts = accounts

    @transactional(datasource="document")
    async def transfer_funds(self, from_id: str, to_id: str, amount: float) -> None:
        source = await self._accounts.find_by_id(from_id)
        target = await self._accounts.find_by_id(to_id)
        source.balance -= amount
        target.balance += amount
        await self._accounts.save_all([source, target])
        # Committed on success; aborted on an exception.
```

**How the manager is found**, per call: `datasource=` (or `manager=`) on the decorator or a class-level `@transactional`; else the legacy `self._motor_client` attribute (an `AsyncMongoClient`, mapped to its manager); else the application's default datasource, which is the document datasource in an application without a relational one (`pyfly.data.document.transaction.default`). A service that exposes both a relational `_session_factory` and a `_motor_client` must name its datasource, or the call raises `IllegalTransactionStateError`.

**Propagation** follows the relational backend:

| Propagation | Inside a unit of this datasource | Outside |
|-------------|----------------------------------|---------|
| `REQUIRED` | joins it | new transaction |
| `REQUIRES_NEW` | suspends it, commits a transaction of its own, resumes it | new transaction |
| `NESTED` | `NestedTransactionNotSupportedError` (MongoDB has no savepoints) | new transaction |
| `SUPPORTS` | joins it | no transaction: repository calls run in short units of their own |
| `NOT_SUPPORTED` | suspends it and runs without one | runs without one |
| `MANDATORY` | joins it | `IllegalTransactionStateError` |
| `NEVER` | `IllegalTransactionStateError` | runs without one |

A unit of another datasource never satisfies a join: a `REQUIRED` MongoDB boundary inside a relational unit starts its own transaction, which commits at its own end whatever the relational unit does next (there is no two-phase commit between them).

**Failures and the rest of the semantics** are those of the unit of work (see [Transaction Management](data-relational.md#transaction-management)): additive `rollback_for`/`no_rollback_for` rules, rollback-only marking (a caught participant failure makes the outer boundary raise `UnexpectedRollbackError`), `read_only=True` (repository writes are refused), `timeout=` (`TransactionTimedOutError`, and `maxCommitTimeMS` on the commit), synchronizations (`after_commit`), cancellation safety (commit, abort and `end_session` run shielded, so a client disconnect never leaves a transaction open on the server holding its document locks). MongoDB aborts a transaction as soon as one of its commands fails, so a caught `DuplicateKeyException` dooms the unit too. A commit whose outcome the driver cannot know raises `CommitOutcomeUnknownError`, never retried.

**Code that calls Beanie or pymongo directly** passes the session on. A coroutine that declares a `session` parameter receives the unit's session there (as it always did), and `current_session()` returns it anywhere inside the unit:

```python
from pyfly.data.document.mongodb import current_session


@transactional(datasource="document")
async def place(self, order: OrderDocument, *, session=None) -> None:
    await order.insert(session=session)
    await InventoryDocument.find_one(InventoryDocument.sku == order.sku, session=current_session()).inc(
        {InventoryDocument.quantity: -order.quantity}, session=current_session()
    )
```

A Beanie call without `session=` runs outside the transaction. `TransactionTemplate("document")` and `infrastructure_unit("document")` work on the document datasource too.

> **Deprecated:** `from pyfly.data.document.mongodb import mongo_transactional` still works but is a thin alias of `@transactional`, and `run_mongo_transaction` a deprecated entry point that runs through the same unit of work. Before 26.09.08, `@transactional` on a document service crashed on every call (`start_transaction()` is a coroutine on pymongo's async API), and repository writes never carried the session.

### Replica Set Requirement

MongoDB transactions require a replica set (a single-node one is enough) or a sharded cluster. On a standalone server `@transactional` raises `IllegalTransactionStateError` saying so, instead of running without a transaction; repository calls outside a transaction still work (each write is atomic on its own document). A message listener container opens a unit per delivery on the default datasource: on a standalone server set `pyfly.messaging.listener.transactional` (`pyfly.eda.listener.transactional`) to `false`.

For local development, you can run a single-node replica set:

```bash
# Start MongoDB as a single-node replica set
mongod --replSet rs0 --bind_ip localhost --port 27017

# Initialize the replica set (run once in mongosh)
rs.initiate({_id: "rs0", members: [{_id: 0, host: "localhost:27017"}]})
```

Or use Docker Compose:

```yaml
version: "3.8"
services:
  mongo:
    image: mongo:7
    command: ["--replSet", "rs0", "--bind_ip_all"]
    ports:
      - "27017:27017"
    healthcheck:
      test: |
        mongosh --eval 'try { rs.status() } catch { rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]}) }'
      interval: 10s
      start_period: 30s
```

In tests, `pyfly.testing.testcontainers.mongodb_replica_set_container()` starts one (MongoDB 7, `rs0`, `directConnection=true`).

### Usage Example

A complete example of transactional order processing, on repositories:

```python
from pyfly.container import service
from pyfly.data import transactional


@service
class OrderService:
    def __init__(self, orders: OrderRepository, inventory: InventoryRepository) -> None:
        self._orders = orders
        self._inventory = inventory

    @transactional(datasource="document")
    async def place_order(self, customer_id: str, product_id: str, quantity: int) -> OrderDocument:
        """Place an order and decrement inventory atomically."""
        stock = await self._inventory.find_by_product_id(product_id)
        if stock is None or stock.quantity < quantity:
            raise ValueError("Insufficient inventory")
        stock.quantity -= quantity
        await self._inventory.save(stock)
        return await self._orders.save(
            OrderDocument(customer_id=customer_id, product_id=product_id, quantity=quantity, status="CONFIRMED")
        )
        # Both writes commit together, or both roll back
```

Source file: `src/pyfly/data/document/mongodb/transaction_manager.py`

---

## MongoRepositoryBeanPostProcessor

The `MongoRepositoryBeanPostProcessor` is a `BeanPostProcessor` that runs after each repository bean is initialized. It scans the repository class for stub methods and replaces them with real MongoDB query implementations. It mirrors the `RepositoryBeanPostProcessor` from the SQLAlchemy adapter but targets MongoDB via Beanie ODM.

### How It Works

The `after_init(bean, bean_name)` method:

1. **Checks the bean type.** If the bean is not an instance of `MongoRepository`, it is returned unchanged.

2. **Identifies custom methods.** Walks the repository class's MRO up to `MongoRepository`: the methods the class declares, and those it inherits from an intermediate base or a mixin (excluding private attributes starting with `_` and methods of the base `MongoRepository`; the most derived definition of a name wins).

3. **Detects derived query methods.** For each method that starts with a recognized prefix (`find_by_`, `count_by_`, `exists_by_`, `delete_by_`) and is a stub, the processor:
   - Parses the method name via `QueryMethodParser.parse()` against the document's fields (`_properties()`), so a name that names no field fails at startup with `InvalidQueryMethodError`.
   - Checks that the stub's parameters match what the name asks for (a mismatch fails at startup with `InvalidQueryMethodError`), and its `Pageable` or `Sort` parameter against what it returns.
   - Compiles the parsed query via `MongoQueryMethodCompiler.compile()`.
   - Wraps it as a repository operation that runs in the call's unit of work (a read unit for `find_by_`/`count_by_`/`exists_by_`, a write for `delete_by_`), taking its arguments by position or by keyword.
   - Replaces the stub method on the bean instance.

4. **Binds the transaction managers.** Every repository resolves its transaction manager from the context's `TransactionManagerRegistry`, so two contexts in one process never share units.

### Stub Detection

A derived-query method is implemented only when its body is a **stub**, recognized by the shape of its body alone (`pyfly.data.post_processor.is_stub`, shared with the SQLAlchemy adapter): an optional docstring, then nothing else, `...`, `pass`, or `raise NotImplementedError`:

```python
async def find_by_status(self, status: str) -> list[OrderDocument]: ...    # Ellipsis stub
async def find_by_status(self, status: str) -> list[OrderDocument]: pass   # Pass stub
```

Any other body is a hand-written implementation and is never replaced, however little it holds (before 26.09.08, a body without a literal constant was replaced).

The post-processor is registered automatically by `DocumentAutoConfiguration.mongo_post_processor()` when the MongoDB subsystem is enabled. No manual registration is required.

Source file: `src/pyfly/data/document/mongodb/post_processor.py`

---

## Pagination

For the full `Pageable`, `Sort`, `Order`, and `Page[T]` API reference, see the [Data Module Guide — Pagination & Sorting](data.md#pagination-sorting).

### Paginated Queries

The `find_all(pageable)` method on `MongoRepository` accepts a `Pageable` object with sorting and returns a `Page[T]`; `find_slice(pageable)` returns a `Slice[T]` without counting:

```python
from pyfly.data import Pageable, Sort

# Basic pagination (page 1, 20 items per page)
page = await repo.find_all(Pageable.of(1, 20))

# With Pageable (page, size, and sorting)
pageable = Pageable.of(
    page=2,
    size=10,
    sort=Sort.by("created_at").descending(),
)
page = await repo.find_all(pageable)

# No count: whether another page follows
window = await repo.find_slice(Pageable.of(1, 50, Sort.by("name")))
```

The `Pageable` carries the `page`, `size`, and `sort` for the query. Pageable in PyFly is 1-based (`page >= 1`).

`find_all(pageable)`:
1. Resolves the sort's field names (`id` to `_id`, aliases; an unknown name raises `InvalidPropertyError`) and appends `_id` after them, so every page is deterministic.
2. Reads the page with `.sort()`, `.skip()` and `.limit()` (one aggregation instead when an order needs computed keys: `nulls_last()` ascending, `nulls_first()` descending, `ignoring_case()`).
3. Counts the matching documents only when the page does not prove the total: a first page shorter than its size, or a short later page, gives it.
4. Returns a `Page[T]` with the items, total count, page number, and size.

`find_slice(pageable)` reads `size + 1` documents instead and returns a `Slice[T]` whose `has_next` says whether the extra one existed.

### Sort Specification Building

`MongoRepository._sort_plan()` translates a `Sort` into a pymongo sort specification and the computed keys it needs. `Sort(orders=(Order.desc("created_at"), Order.asc("name")))` becomes:

```python
[("created_at", pymongo.DESCENDING), ("name", pymongo.ASCENDING)]
```

and `Sort.by(Order.asc("score").nulls_last(), Order.asc("title").ignoring_case())` sorts in an aggregation on:

```python
{"$addFields": {
    "__pyfly_null_0": {"$cond": [{"$in": [{"$type": "$score"}, ["null", "missing"]]}, 1, 0]},
    "__pyfly_sort_1": {"$cond": [{"$eq": [{"$type": "$title"}, "string"]}, {"$toLower": "$title"}, "$title"]},
}}
[("__pyfly_null_0", 1), ("score", 1), ("__pyfly_sort_1", 1)]
```

---

## Integration with Web Layer

### Controller with Valid[T] and MongoRepository

The MongoDB adapter integrates seamlessly with PyFly's web layer. Here is a complete example showing a controller that uses `Valid[T]` for request validation and a `MongoRepository` for data access:

```python
# --- Document ---

from pyfly.data.document.mongodb import BaseDocument
from beanie import Indexed
from pydantic import Field


class TaskDocument(BaseDocument):
    title: str
    description: str = ""
    priority: Indexed(int) = 0
    status: str = "TODO"
    assignee: str | None = None

    class Settings:
        name = "tasks"


# --- Repository ---

from pyfly.data.document.mongodb import MongoRepository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class TaskRepository(MongoRepository[TaskDocument, str]):

    async def find_by_status(self, status: str) -> list[TaskDocument]: ...

    async def find_by_assignee_and_status(
        self, assignee: str, status: str
    ) -> list[TaskDocument]: ...

    async def find_by_priority_greater_than_order_by_priority_desc(
        self, min_priority: int
    ) -> list[TaskDocument]: ...

    async def count_by_status(self, status: str) -> int: ...


# --- Request/Response Models ---

from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = ""
    priority: int = Field(0, ge=0, le=10)
    assignee: str | None = None


class TaskResponse(BaseModel):
    id: str
    title: str
    description: str
    priority: int
    status: str
    assignee: str | None


# --- Service ---

from pyfly.container import service
from pyfly.data import Mapper


@service
class TaskService:
    def __init__(self, repo: TaskRepository) -> None:
        self._repo = repo
        self._mapper = Mapper()

    async def create(self, request: CreateTaskRequest) -> TaskResponse:
        doc = TaskDocument(
            title=request.title,
            description=request.description,
            priority=request.priority,
            assignee=request.assignee,
        )
        saved = await self._repo.save(doc)
        return TaskResponse(
            id=str(saved.id),
            title=saved.title,
            description=saved.description,
            priority=saved.priority,
            status=saved.status,
            assignee=saved.assignee,
        )

    async def find_by_id(self, task_id: str) -> TaskResponse | None:
        doc = await self._repo.find_by_id(task_id)
        if doc is None:
            return None
        return TaskResponse(
            id=str(doc.id),
            title=doc.title,
            description=doc.description,
            priority=doc.priority,
            status=doc.status,
            assignee=doc.assignee,
        )

    async def find_by_status(self, status: str) -> list[TaskResponse]:
        docs = await self._repo.find_by_status(status)
        return [
            TaskResponse(
                id=str(d.id), title=d.title, description=d.description,
                priority=d.priority, status=d.status, assignee=d.assignee,
            )
            for d in docs
        ]


# --- Controller ---

from pyfly.container import rest_controller
from pyfly.kernel.exceptions import ResourceNotFoundException
from pyfly.web import (
    request_mapping, get_mapping, post_mapping, delete_mapping,
    exception_handler, PathVar, QueryParam, Valid,
)


@rest_controller
@request_mapping("/api/tasks")
class TaskController:
    def __init__(self, task_service: TaskService) -> None:
        self._service = task_service

    @get_mapping("/")
    async def list_tasks(self, status: QueryParam[str] = None) -> list[TaskResponse]:
        if status:
            return await self._service.find_by_status(status)
        return await self._service.find_by_status("TODO")

    @get_mapping("/{task_id}")
    async def get_task(self, task_id: PathVar[str]) -> TaskResponse:
        task = await self._service.find_by_id(task_id)
        if task is None:
            raise ResourceNotFoundException(f"Task {task_id} not found")
        return task

    @post_mapping("/", status_code=201)
    async def create_task(self, body: Valid[CreateTaskRequest]) -> TaskResponse:
        return await self._service.create(body)

    @exception_handler(ResourceNotFoundException)
    async def handle_not_found(self, exc: ResourceNotFoundException):
        return 404, {"error": {"message": str(exc), "code": "TASK_NOT_FOUND"}}
```

---

## Complete CRUD Example

The following example demonstrates a full Product document, repository with derived queries, service, and controller — a complete vertical slice of a PyFly application using MongoDB.

```python
# ==========================================================================
# Document
# ==========================================================================

from pyfly.data.document.mongodb import BaseDocument
from beanie import Indexed, PydanticObjectId
from pydantic import Field


class ProductDocument(BaseDocument):
    """Product stored in the 'products' MongoDB collection."""

    name: str
    sku: Indexed(str, unique=True)
    description: str = ""
    price: float = Field(gt=0)
    category: Indexed(str)
    tags: list[str] = Field(default_factory=list)
    active: bool = True

    class Settings:
        name = "products"


# ==========================================================================
# Repository
# ==========================================================================

from pyfly.data.document.mongodb import MongoRepository
from pyfly.container import repository as repo_stereotype


@repo_stereotype
class ProductRepository(MongoRepository[ProductDocument, PydanticObjectId]):

    # --- Derived query methods (stubs, auto-compiled at startup) ---

    # Equals (default operator)
    async def find_by_category(self, category: str) -> list[ProductDocument]: ...

    # AND connector
    async def find_by_active_and_category(
        self, active: bool, category: str
    ) -> list[ProductDocument]: ...

    # Greater than + ordering
    async def find_by_price_greater_than_order_by_price_desc(
        self, min_price: float
    ) -> list[ProductDocument]: ...

    # Contains, whatever the case
    async def find_by_name_containing_ignore_case(self, fragment: str) -> list[ProductDocument]: ...

    # Count
    async def count_by_category(self, category: str) -> int: ...

    # Exists
    async def exists_by_sku(self, sku: str) -> bool: ...

    # Delete
    async def delete_by_active(self, active: bool) -> int: ...


# ==========================================================================
# Request / Response Models
# ==========================================================================

from pydantic import BaseModel


class CreateProductRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    sku: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    price: float = Field(..., gt=0)
    category: str
    tags: list[str] = Field(default_factory=list)


class UpdateProductRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    price: float = Field(..., gt=0)
    category: str
    tags: list[str] = Field(default_factory=list)


class ProductResponse(BaseModel):
    id: str
    name: str
    sku: str
    description: str
    price: float
    category: str
    tags: list[str]
    active: bool


# ==========================================================================
# Service
# ==========================================================================

from pyfly.container import service
from pyfly.data import Page, Pageable, Sort
from pyfly.kernel.exceptions import ResourceNotFoundException, ConflictException


@service
class ProductService:
    def __init__(self, repo: ProductRepository) -> None:
        self._repo = repo

    async def create(self, request: CreateProductRequest) -> ProductResponse:
        # Check for duplicate SKU
        if await self._repo.exists_by_sku(request.sku):
            raise ConflictException(
                f"Product with SKU '{request.sku}' already exists",
                code="DUPLICATE_SKU",
            )

        doc = ProductDocument(
            name=request.name,
            sku=request.sku,
            description=request.description,
            price=request.price,
            category=request.category,
            tags=request.tags,
        )
        saved = await self._repo.save(doc)
        return self._to_response(saved)

    async def find_by_id(self, product_id: str) -> ProductResponse:
        doc = await self._repo.find_by_id(product_id)
        if doc is None:
            raise ResourceNotFoundException(
                f"Product {product_id} not found",
                code="PRODUCT_NOT_FOUND",
            )
        return self._to_response(doc)

    async def find_all_active(
        self, category: str | None = None
    ) -> list[ProductResponse]:
        if category:
            docs = await self._repo.find_by_active_and_category(True, category)
        else:
            docs = await self._repo.find_all(active=True)
        return [self._to_response(d) for d in docs]

    async def find_paginated(
        self,
        page: int = 1,
        size: int = 20,
    ) -> Page[ProductResponse]:
        pageable = Pageable.of(
            page=page,
            size=size,
            sort=Sort.by("name"),
        )
        result = await self._repo.find_all(pageable)
        return result.map(self._to_response)

    async def search_by_name(self, query: str) -> list[ProductResponse]:
        docs = await self._repo.find_by_name_containing_ignore_case(query)
        return [self._to_response(d) for d in docs]

    async def delete(self, product_id: str) -> None:
        await self._repo.delete_by_id(product_id)

    @staticmethod
    def _to_response(doc: ProductDocument) -> ProductResponse:
        return ProductResponse(
            id=str(doc.id),
            name=doc.name,
            sku=doc.sku,
            description=doc.description,
            price=doc.price,
            category=doc.category,
            tags=doc.tags,
            active=doc.active,
        )


# ==========================================================================
# Controller
# ==========================================================================

from pyfly.container import rest_controller
from pyfly.web import (
    request_mapping, get_mapping, post_mapping, put_mapping, delete_mapping,
    exception_handler, Body, PathVar, QueryParam, Valid,
)


@rest_controller
@request_mapping("/api/products")
class ProductController:

    def __init__(self, product_service: ProductService) -> None:
        self._service = product_service

    @get_mapping("/")
    async def list_products(
        self,
        category: QueryParam[str] = None,
    ) -> list[ProductResponse]:
        """List active products, optionally filtered by category."""
        return await self._service.find_all_active(category=category)

    @get_mapping("/{product_id}")
    async def get_product(self, product_id: PathVar[str]) -> ProductResponse:
        """Get a product by its ID."""
        return await self._service.find_by_id(product_id)

    @get_mapping("/search")
    async def search_products(
        self, q: QueryParam[str] = "",
    ) -> list[ProductResponse]:
        """Search products by name (case-insensitive contains)."""
        return await self._service.search_by_name(q)

    @post_mapping("/", status_code=201)
    async def create_product(self, body: Valid[CreateProductRequest]) -> ProductResponse:
        """Create a new product with Pydantic validation."""
        return await self._service.create(body)

    @delete_mapping("/{product_id}", status_code=204)
    async def delete_product(self, product_id: PathVar[str]) -> None:
        """Delete a product by ID."""
        await self._service.delete(product_id)

    # --- Exception Handlers ---

    @exception_handler(ResourceNotFoundException)
    async def handle_not_found(self, exc: ResourceNotFoundException):
        return 404, {
            "error": {
                "message": str(exc),
                "code": exc.code or "NOT_FOUND",
            }
        }

    @exception_handler(ConflictException)
    async def handle_conflict(self, exc: ConflictException):
        return 409, {
            "error": {
                "message": str(exc),
                "code": exc.code or "CONFLICT",
            }
        }


# ==========================================================================
# Application Bootstrap
# ==========================================================================

from pyfly.core import pyfly_application, PyFlyApplication
from pyfly.web.adapters.starlette import create_app


@pyfly_application(
    name="product-service",
    version="1.0.0",
    scan_packages=["product_service"],
    description="Product catalog microservice backed by MongoDB",
)
class Application:
    pass


async def main():
    pyfly_app = PyFlyApplication(Application)
    await pyfly_app.startup()

    # DocumentAutoConfiguration (discovered via pyfly.auto_configuration entry points)
    # automatically registers and starts:
    #   - mongo_client        (AsyncMongoClient, from pyfly.data.document.*)
    #   - mongo_post_processor (MongoRepositoryBeanPostProcessor)
    #   - odm_initializer     (BeanieInitializer — binds the repositories' documents
    #                           and calls init_beanie() when the lifecycle beans start)
    #   - mongo_transaction_manager, mongo_health_indicator, document_auditing_handler

    # Create the web application
    app = create_app(
        title="Product Catalog",
        version="1.0.0",
        description="CRUD API for product management with MongoDB",
        context=pyfly_app.context,
        docs_enabled=True,
        actuator_enabled=True,
    )

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
```

**Configuration file (`pyfly.yaml`):**

```yaml
pyfly:
  app:
    name: product-service
    version: 1.0.0
    description: Product catalog microservice backed by MongoDB

  data:
    document:
      enabled: true
      uri: mongodb://localhost:27017
      database: product_catalog
      min_pool_size: 5
      max_pool_size: 50

  web:
    port: 8080
    docs:
      enabled: true
    actuator:
      enabled: true
```

---

## See Also

- [Data Module Guide](data.md) — Generic commons: repository ports, pagination, query parsing, entity mapping, extensibility
- [Data Relational Guide](data-relational.md) — SQLAlchemy adapter
- [MongoDB Adapter Reference](../adapters/mongodb.md) — Setup, configuration, adapter-specific features
