<span class="eyebrow">Appendix B</span>

# MongoDB & Document Data {.chtitle}

PyFly's document data layer wraps MongoDB through **Beanie ODM** and PyMongo's
asynchronous API (**`AsyncMongoClient`**). The API mirrors the relational adapter deliberately — the same
`MongoRepository[T, ID]` base class, the same derived query naming convention, the same
`Page`/`Pageable`/`Sort` vocabulary — so switching between relational and document
storage touches only the document class definition and the repository base class, not
the service layer. It runs on the same unit-of-work model, too: `@transactional`, the
rollback rules and the propagations work the same way (except `NESTED`: MongoDB has no
savepoints).

All concrete types live in `pyfly.data.document.mongodb`. Shared types (`Page`,
`Pageable`, `Sort`) come from `pyfly.data`.

---

## Installation and configuration

Install the extra:

::: listing terminal | Listing B.1 — Install the data-document extra
uv add "pyfly[data-document]"
:::

Enable the adapter in `pyfly.yaml`:

::: listing pyfly.yaml | Listing B.2 — Minimal MongoDB configuration
pyfly:
  data:
    document:
      enabled: true
      uri: "mongodb://localhost:27017"
      database: "myapp"
      min_pool_size: 5
      max_pool_size: 50
:::

### Configuration reference

| `pyfly.yaml` key | Type | Default | Description |
|---|---|---|---|
| `pyfly.data.document.enabled` | bool | `false` | Enable the MongoDB adapter |
| `pyfly.data.document.uri` | str | `mongodb://localhost:27017` | Connection URI |
| `pyfly.data.document.database` | str | `pyfly` | Database name |
| `pyfly.data.document.min_pool_size` | int | `0` | Connection pool minimum (`minPoolSize`) |
| `pyfly.data.document.max_pool_size` | int | `100` | Connection pool maximum (`maxPoolSize`) |
| `pyfly.data.document.datasource` | str | `document` | Datasource name of the document units of work |
| `pyfly.data.document.tz_aware` | bool | `true` | Datetimes come back as aware UTC values |
| `pyfly.data.document.transaction.default` | bool | unset | Whether the document datasource is the default of `@transactional` (by default, when the relational layer is off) |

Every key has a matching environment variable: replace dots with underscores and
uppercase — e.g. `PYFLY_DATA_DOCUMENT_URI`. For MongoDB Atlas or a replica set:

::: listing pyfly.yaml | Listing B.3 — Atlas and replica-set URIs
# Atlas
pyfly:
  data:
    document:
      enabled: true
      uri: >-
        mongodb+srv://user:secret@cluster.mongodb.net/
        ?retryWrites=true&w=majority
      database: production_db

# Replica set (required for transactions)
# pyfly:
#   data:
#     document:
#       uri: "mongodb://m1:27017,m2:27017,m3:27017/?replicaSet=rs0"
:::

---

## BaseDocument

`BaseDocument` extends `beanie.Document` with an audit trail. Every document class in
a PyFly application should inherit from it.

| Field | Type | Default | Description |
|---|---|---|---|
| `id` | `PydanticObjectId` | Auto-generated | Document primary key (ObjectId) |
| `created_at` | `datetime` | `datetime.now(UTC)` | Insert timestamp |
| `updated_at` | `datetime` | `datetime.now(UTC)` | Last-update timestamp |
| `created_by` | `str \| None` | `None` | Creator identifier |
| `updated_by` | `str \| None` | `None` | Last-updater identifier |

`use_state_management = True` is set on the base `Settings` class, enabling Beanie's
change-tracking so `save_changes()` produces efficient partial updates.

A typical document class:

::: listing catalog/product_document.py | Listing B.4 — ProductDocument with index and nested model
from pydantic import BaseModel, Field
from beanie import Indexed, PydanticObjectId
from pyfly.data.document.mongodb import BaseDocument


class Dimensions(BaseModel):
    width_cm: float
    height_cm: float
    depth_cm: float


class ProductDocument(BaseDocument):
    name: str
    sku: Indexed(str, unique=True)
    description: str = ""
    price: float = Field(gt=0)
    category: Indexed(str)
    tags: list[str] = Field(default_factory=list)
    dimensions: Dimensions | None = None
    active: bool = True

    class Settings:
        name = "products"
:::

The `Settings.name` attribute sets the MongoDB collection name. Omitting it causes
Beanie to derive the name from the class name, which is rarely what you want.

For compound or descending indexes use `Settings.indexes`:

::: listing catalog/product_document.py | Listing B.5 — Compound index via Settings.indexes
from pymongo import IndexModel, ASCENDING, DESCENDING
from pyfly.data.document.mongodb import BaseDocument


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
:::

---

## Spring Data ↔ MongoRepository mapping

The table below shows how Spring Data MongoDB concepts map to PyFly's document layer.
The surface is intentionally identical to `Repository[T, ID]` in the relational adapter,
so the same service-layer patterns apply to both.

| Spring Data MongoDB | PyFly | Notes |
|---|---|---|
| `MongoRepository<E, ID>` | `MongoRepository[E, ID]` | `from pyfly.data.document.mongodb import MongoRepository`; decorate with `@repository`. |
| `@Document class Product` + `@Id` | `class ProductDocument(BaseDocument)` | `from pyfly.data.document.mongodb import BaseDocument`. Inherits `id` (Beanie `PydanticObjectId`), `created_at`, `updated_at`, `created_by`, `updated_by`. Collection name set in `class Settings: name = "products"`. |
| `findByCategory(String c)` | `async def find_by_category(self, category: str) -> list[ProductDocument]: ...` | Stub body `...` compiled by `MongoRepositoryBeanPostProcessor` at startup. Same prefixes as relational: `find_by_`, `count_by_`, `exists_by_`, `delete_by_`. |
| `@Query("{ 'status': ?0 }")` | `@query('{"status": ":status"}')` | `from pyfly.data.query import query`. JSON filter or aggregation pipeline; `:param` substitution. |
| `MongoSpecification` | `MongoSpecification(lambda root, q: {"active": True})` | `from pyfly.data.document.mongodb import MongoSpecification`. Compose with `&` / `\|` / `~`; run via `find_all_by_spec(spec)` or `find_all_by_spec_paged(spec, pageable)`. |
| `PageRequest.of(page, size, Sort.by(…))` | `Pageable.of(page, size, Sort.by("name").descending())` | `from pyfly.data import Pageable, Sort` — identical to the relational adapter. |
| `Page<T>` | `Page[T]` | `.items`, `.total`, `.page`, `.size`, `.total_pages`, `.map(fn)` — same as relational. |

---

## MongoRepository[T, ID]

Subclass `MongoRepository[T, ID]` and annotate with `@repository`. The framework
extracts the document type and ID type from the generic parameters at class-definition
time via `__init_subclass__`. No `__init__` is required.

::: listing catalog/product_repository.py | Listing B.6 — ProductRepository: CRUD + derived queries
from beanie import PydanticObjectId
from pyfly.container import repository
from pyfly.data.document.mongodb import MongoRepository

from catalog.product_document import ProductDocument


@repository
class ProductRepository(MongoRepository[ProductDocument, PydanticObjectId]):

    # --- derived query method stubs (compiled at startup) ---

    async def find_by_category(
        self, category: str
    ) -> list[ProductDocument]: ...

    async def find_by_active_and_category(
        self, active: bool, category: str
    ) -> list[ProductDocument]: ...

    async def find_by_price_greater_than_order_by_price_desc(
        self, min_price: float
    ) -> list[ProductDocument]: ...

    async def find_by_name_containing(
        self, fragment: str
    ) -> list[ProductDocument]: ...

    async def count_by_category(self, category: str) -> int: ...

    async def exists_by_sku(self, sku: str) -> bool: ...

    async def delete_by_active(self, active: bool) -> int: ...
:::

### Built-in CRUD methods

| Method | Return type | Description |
|---|---|---|
| `save(entity)` | `T` | Insert a new document (`insert_one`) or update a stored one, in one command |
| `find_by_id(id)` | `T \| None` | Find by primary key |
| `find_all(**filters)` | `list[T]` | Find all; keyword args become equality filters |
| `find_all(sort)` | `list[T]` | Fetch all documents, ordered by a `Sort` |
| `find_all(pageable)` | `Page[T]` | Paged query: counts the total, applies the Pageable's sort, slices with skip/limit, returns `Page[T]` |
| `stream_all(sort)` | `AsyncIterator[T]` | Stream all documents (the `Flux<T>` analogue); optional `Sort` and equality filters |
| `delete(entity)` | `None` | Delete a loaded document instance |
| `delete_by_id(id)` | `None` | Delete by primary key; no-op if not found |
| `count()` | `int` | Count all documents in the collection |
| `exists_by_id(id)` | `bool` | True if a document with this ID exists |
| `save_all(entities)` | `list[T]` | One ordered bulk write for new and stored documents (atomic on a replica set) |
| `find_all_by_id(ids)` | `list[T]` | Find all with IDs in a list |
| `delete_all_by_id(ids)` | `None` | Delete all with IDs in a list |
| `delete_all(entities=None)` | `None` | Delete the given documents; with no args, truncate the entire collection |
| `delete_all_in_batch(entities=None)` | `None` | Bulk delete that bypasses delete event actions |
| `find_all_by_spec(spec)` | `list[T]` | Find matching a `MongoSpecification` |
| `find_all_by_spec_paged(spec, pageable)` | `Page[T]` | Find matching a `MongoSpecification` with pagination and sort |

`find_all(**filters)` translates keyword arguments into MongoDB equality filters:

```python
# {"status": "PENDING", "customer_id": "abc"}
orders = await repo.find_all(status="PENDING", customer_id="abc")
```

The repository never holds a session. Inside a unit of work of its datasource (`@transactional`, a message delivery) every call passes the unit's session to the driver, so its writes are part of the transaction. Outside one, each call runs in a short unit of its own: a read without a transaction, a write that sends one command (`save`, `delete`) on its own, and a method that writes more than once (`save_all`) in a transaction that commits at the end of the call. A failed save gives the documents back the id and revision they had, so the same objects can be saved again; a new document is inserted with `insert_one`, so move logic from an `insert`/`save` override into `@before_event`/`@after_event` actions.

---

## Derived query methods

PyFly compiles stub methods on `MongoRepository` subclasses into real MongoDB queries
at startup. The naming convention is identical to the relational adapter and to Spring
Data: `{prefix}_by_{predicates}[_order_by_{fields}]`.

**Prefixes:** `find_by`, `count_by`, `exists_by`, `delete_by`

**Connectors:** `_and_`, `_or_`

### Operator mapping

| Method suffix | MongoDB filter | Args consumed |
|---|---|---|
| *(none, default)* | `{field: value}` | 1 |
| `_not` | `{field: {"$nin": [value, None]}}` (null is never a match, as on SQL) | 1 |
| `_greater_than` | `{field: {"$gt": value}}` | 1 |
| `_greater_than_equal` | `{field: {"$gte": value}}` | 1 |
| `_less_than` | `{field: {"$lt": value}}` | 1 |
| `_less_than_equal` | `{field: {"$lte": value}}` | 1 |
| `_between` | `{field: {"$gte": low, "$lte": high}}` | 2 |
| `_like` | `{field: {"$regex": "^...$"}}` (SQL `LIKE`: anchored, case-sensitive) | 1 |
| `_containing` | `{field: {"$regex": "<escaped value>"}}` (case-sensitive) | 1 |
| `_in` | `{field: {"$in": values}}` | 1 (list) |
| `_is_null` | `{field: None}` | 0 |
| `_is_not_null` | `{field: {"$ne": None}}` | 0 |

Add `_ignore_case` after a predicate for a case-insensitive match. (Before v26.09.08, `_containing` ignored case and `_like` was unanchored.) Ordering: append `_order_by_{field}_{asc|desc}`. Multiple sort fields are chained:

```python
# sort=[("name", ASC), ("created_at", DESC)]
async def find_by_active_order_by_name_asc_created_at_desc(
    self, active: bool
) -> list[ProductDocument]: ...
```

The `MongoRepositoryBeanPostProcessor` detects stubs (bodies containing only `...`
or `pass`) and replaces them with compiled callables. Source:
`src/pyfly/data/document/mongodb/post_processor.py` and
`src/pyfly/data/document/mongodb/query_compiler.py`.

---

## Custom queries with @query

For queries that cannot be expressed through naming conventions, `@query` accepts a
MongoDB filter document (`{…}`) or aggregation pipeline (`[…]`) as a JSON string.
Named parameters use `:param_name` syntax.

::: listing catalog/order_repository.py | Listing B.7 — @query filter and aggregation examples
from pyfly.container import repository
from pyfly.data.document.mongodb import MongoRepository
from pyfly.data.query import query

from catalog.order_document import OrderDocument


@repository
class OrderRepository(MongoRepository[OrderDocument, str]):

    @query('{"status": ":status", "total": {"$gte": ":min_total"}}')
    async def find_by_status_min_total(
        self, status: str, min_total: float
    ) -> list[OrderDocument]: ...

    @query(
        '[{"$match": {"customer_id": ":cid"}},'
        ' {"$group": {"_id": "$category",'
        '             "total": {"$sum": "$amount"}}}]'
    )
    async def totals_by_category(
        self, cid: str
    ) -> list[dict]: ...
:::

**Substitution rules:**

- A JSON string value that is *exactly* `:param_name` is replaced by the Python value,
  preserving its type (`int`, `bool`, `list`, etc.).
- `:param_name` embedded inside a larger string is replaced via `str(value)`.
- Dicts and lists are recursed. Non-string JSON values pass through unchanged.

`MongoQueryExecutor` parses the query template once at startup, detects whether it
is a filter or pipeline, and substitutes parameters at call time.

---

## Pagination

::: listing catalog/product_service.py | Listing B.8 — Paginated product listing
from pyfly.data import Page, Pageable, Sort
from pyfly.data.document.mongodb import MongoRepository

from catalog.product_document import ProductDocument


async def list_products(
    repo: MongoRepository[ProductDocument, str],
    page: int = 1,
    size: int = 20,
) -> Page[ProductDocument]:
    pageable = Pageable.of(
        page=page,
        size=size,
        sort=Sort.by("name"),
    )
    return await repo.find_all(pageable)
:::

`find_all(pageable)` counts the total, applies the Pageable's sort, slices with
`.skip((page-1)*size)` and `.limit(size)` on the Beanie query, and returns `Page[T]`.
Pageable is 1-based, so page `1` is the first page.

---

## Transaction management

Multi-document transactions require a **replica set** deployment — a single-node one is
enough. Standalone MongoDB does not support them: there `@transactional` raises
`IllegalTransactionStateError` instead of running without a transaction.

::: listing pyfly.yaml | Listing B.9 — Single-node replica set for local development
# Start MongoDB: mongod --replSet rs0 --bind_ip localhost
# Init (once, in mongosh): rs.initiate()
pyfly:
  data:
    document:
      enabled: true
      uri: "mongodb://localhost:27017/?replicaSet=rs0"
      database: myapp
:::

MongoDB uses the **same** `@transactional` as the relational adapter. Its
`MongoTransactionManager` binds a pymongo `ClientSession` to the running task, and every
repository call inside the method passes it to the driver:

::: listing billing/transfer.py | Listing B.10 — Atomic fund transfer with @transactional
from pyfly.container import service
from pyfly.data import transactional

from billing.account_repository import AccountRepository


@service
class TransferService:
    def __init__(self, accounts: AccountRepository) -> None:
        self._accounts = accounts

    @transactional(datasource="document")
    async def transfer(
        self, from_id: str, to_id: str, amount: float
    ) -> None:
        src = await self._accounts.find_by_id(from_id)
        dst = await self._accounts.find_by_id(to_id)
        if src is None or dst is None or src.balance < amount:
            raise ValueError("Invalid transfer")
        src.balance -= amount
        dst.balance += amount
        await self._accounts.save(src)
        await self._accounts.save(dst)  # a failure here undoes the debit
:::

On success the transaction commits; on any exception it aborts and re-raises. `datasource="document"`
names the document datasource; in an application without a relational layer it is the default,
and a bare `@transactional` finds it. A service with both a relational `_session_factory` and a
`_motor_client` must name its datasource, or the call raises `IllegalTransactionStateError`. The
rest follows the relational rules: `REQUIRES_NEW` suspends the unit, `NESTED` raises
`NestedTransactionNotSupportedError`, only `Isolation.DEFAULT` is accepted, a caught failure of a
participant makes the commit raise `UnexpectedRollbackError`, and a commit whose outcome the driver
cannot know raises `CommitOutcomeUnknownError`. Code that calls Beanie or pymongo directly passes
the session on: a `session` parameter receives it, and `current_session()` returns it.

The transactional outbox runs on MongoDB as well: with `pyfly.eda.outbox.enabled: true`, a
MongoDB-only application appends its events to `pyfly_outbox_*` collections in the unit's own
transaction (`pyfly.eda.outbox.store: mongo`, which `auto` picks there).

!!! warning "Replica set required"
    `@transactional` raises `IllegalTransactionStateError` against a standalone MongoDB instance.
    Use the `?replicaSet=rs0` URI fragment (see Listing B.9) even for local dev. In a MongoDB-only
    application on a standalone server, also set `pyfly.messaging.listener.transactional: false`:
    each message delivery would otherwise open a document unit and fail.

!!! note "Motor is gone"
    Since v26.09.08 only PyMongo's `AsyncMongoClient` is accepted: the transaction manager refuses
    a Motor or mongomock client with `TypeError`. `mongo_transactional` still imports, as a
    deprecated alias of `@transactional`.

---

## Auto-configuration

`DocumentAutoConfiguration` activates when:

1. `beanie` is importable (`@conditional_on_class("beanie")`), and
2. `pyfly.data.document.enabled` is `"true"` in config.

It registers these beans automatically:

| Bean | Type | Role |
|---|---|---|
| `mongo_client` | `AsyncMongoClient` | The client and its connection pool (your own singleton `AsyncMongoClient` bean replaces it) |
| `mongo_post_processor` | `MongoRepositoryBeanPostProcessor` | Compiles derived query stubs |
| `odm_initializer` | `BeanieInitializer` | Calls `init_beanie()` at startup |
| `mongo_transaction_manager` | `MongoTransactionManager` | Runs the document units of work |
| `mongo_health_indicator` | `MongoHealthIndicator` | Readiness check (`ping`, 2 s) |

`BeanieInitializer` discovers the document classes itself: the document of every
`MongoRepository` bean, every Beanie document registered in the container, the classes
or modules listed in `pyfly.data.document.models`, and the documents their `Link` fields
name. This means defining a repository is sufficient — you do not need to register
document models separately.

Source files: `src/pyfly/data/document/auto_configuration.py`,
`src/pyfly/data/document/mongodb/initializer.py`.

---

## Testing

Test transactional code against a real replica set. `mongodb_replica_set_container()`
starts a single-node `rs0` replica set in Docker, and `pyfly_config` turns the document
layer on with its URI:

::: listing tests/test_transfer.py | Listing B.11 — A transaction test on a replica set
import pytest

from pyfly.testing import (
    data_slice,
    mongodb_replica_set_container,
    pyfly_config,
    requires_docker,
)

from billing.account_document import AccountDocument
from billing.account_repository import AccountRepository
from billing.transfer import TransferService


@pytest.fixture(scope="module")
def mongo():
    with mongodb_replica_set_container() as container:
        yield container


@requires_docker
async def test_transfer_commits_both_sides(mongo) -> None:
    config = pyfly_config(
        mongo, base={"pyfly.data.document.database": "billing_test"}
    )
    async with await data_slice(
        AccountRepository, TransferService, config=config
    ) as ctx:
        accounts = ctx.get_bean(AccountRepository)
        transfers = ctx.get_bean(TransferService)
        await accounts.delete_all()
        src = await accounts.save(AccountDocument(balance=100))
        dst = await accounts.save(AccountDocument(balance=0))

        await transfers.transfer(str(src.id), str(dst.id), 60)
        with pytest.raises(ValueError):
            await transfers.transfer(str(src.id), str(dst.id), 60)

        assert (await accounts.find_by_id(src.id)).balance == 40
        assert (await accounts.find_by_id(dst.id)).balance == 60
:::

`@requires_docker` skips the test where Docker is not available. The slice's rollback
covers relational datasources only, so the test clears its collection first. Install the
support with `pip install 'pyfly[testcontainers]'`; mongomock and Motor are no longer
supported.
