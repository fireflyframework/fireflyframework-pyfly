<span class="eyebrow">Appendix B</span>

# MongoDB & Document Data {.chtitle}

PyFly's document data layer wraps MongoDB through **Beanie ODM** and **Motor** (the
async MongoDB driver). The API mirrors the relational adapter deliberately — the same
`RepositoryPort[T, ID]` contract, the same derived query naming convention, the same
`Page`/`Pageable`/`Sort` vocabulary — so switching between relational and document
storage touches only the document class definition and the repository base class, not
the service layer.

All concrete types live in `pyfly.data.document.mongodb`. Shared types (`Page`,
`Pageable`, `RepositoryPort`, `Sort`) come from `pyfly.data`.

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
| `pyfly.data.document.min_pool_size` | int | `0` | Motor pool minimum |
| `pyfly.data.document.max_pool_size` | int | `100` | Motor pool maximum |

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
| `save(entity)` | `T` | Insert or update via Beanie `entity.save()` |
| `find_by_id(id)` | `T \| None` | Find by primary key |
| `find_all(**filters)` | `list[T]` | Find all; keyword args become equality filters |
| `delete(id)` | `None` | Delete by primary key; no-op if not found |
| `count()` | `int` | Count all documents in the collection |
| `exists(id)` | `bool` | True if a document with this ID exists |
| `find_paginated(page, size, pageable)` | `Page[T]` | Paginated query with optional sort |
| `save_all(entities)` | `list[T]` | Bulk insert via `insert_many` |
| `find_all_by_ids(ids)` | `list[T]` | Find all with IDs in a list |
| `delete_all(ids)` | `int` | Delete all with IDs in a list |
| `find_all_by_spec(spec)` | `list[T]` | Find matching a `MongoSpecification` |

`find_all(**filters)` translates keyword arguments into MongoDB equality filters:

```python
# {"status": "PENDING", "customer_id": "abc"}
orders = await repo.find_all(status="PENDING", customer_id="abc")
```

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
| `_not` | `{field: {"$ne": value}}` | 1 |
| `_greater_than` | `{field: {"$gt": value}}` | 1 |
| `_greater_than_equal` | `{field: {"$gte": value}}` | 1 |
| `_less_than` | `{field: {"$lt": value}}` | 1 |
| `_less_than_equal` | `{field: {"$lte": value}}` | 1 |
| `_between` | `{field: {"$gte": low, "$lte": high}}` | 2 |
| `_like` | `{field: {"$regex": pattern}}` (SQL % → .*) | 1 |
| `_containing` | `{field: {"$regex": ".*val.*", "$options": "i"}}` | 1 |
| `_in` | `{field: {"$in": values}}` | 1 (list) |
| `_is_null` | `{field: None}` | 0 |
| `_is_not_null` | `{field: {"$ne": None}}` | 0 |

Ordering: append `_order_by_{field}_{asc|desc}`. Multiple sort fields are chained:

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
    return await repo.find_paginated(pageable=pageable)
:::

`find_paginated` counts total documents, calculates offset as `(page-1)*size`, applies
`.sort()`, `.skip()`, and `.limit()` to the Beanie query, and returns `Page[T]`.

---

## Transaction management

Multi-document transactions require a **replica set** deployment. Standalone MongoDB
does not support them.

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

The `@mongo_transactional` decorator wraps an async function in a Motor session and
transaction. The function's Beanie operations participate automatically:

::: listing billing/transfer.py | Listing B.10 — Atomic fund transfer with @mongo_transactional
from motor.motor_asyncio import AsyncIOMotorClient
from pyfly.data.document.mongodb import mongo_transactional

from billing.account_document import AccountDocument


def make_transfer_fn(client: AsyncIOMotorClient):
    @mongo_transactional(client)
    async def transfer(
        from_id: str, to_id: str, amount: float
    ) -> None:
        src = await AccountDocument.get(from_id)
        dst = await AccountDocument.get(to_id)
        if src is None or dst is None or src.balance < amount:
            raise ValueError("Invalid transfer")
        src.balance -= amount
        dst.balance += amount
        await src.save()
        await dst.save()
    return transfer
:::

On success the transaction commits; on any exception it aborts and re-raises. Unlike
the relational `@reactive_transactional`, the Motor session is not injected as an
argument — Beanie picks it up through the Motor context.

The `motor_client` bean is registered automatically by `DocumentAutoConfiguration`
when the adapter is enabled. Inject it into your service via the DI container.

!!! warning "Replica set required"
    `@mongo_transactional` will raise an error against a standalone MongoDB instance.
    Use the `?replicaSet=rs0` URI fragment (see Listing B.9) even for local dev.

---

## Auto-configuration

`DocumentAutoConfiguration` activates when:

1. `beanie` is importable (`@conditional_on_class("beanie")`), and
2. `pyfly.data.document.enabled` is `"true"` in config.

It registers three beans automatically:

| Bean | Type | Role |
|---|---|---|
| `motor_client` | `AsyncIOMotorClient` | Async MongoDB connection pool |
| `mongo_post_processor` | `MongoRepositoryBeanPostProcessor` | Compiles derived query stubs |
| `odm_initializer` | `BeanieInitializer` | Calls `init_beanie()` at startup |

`BeanieInitializer.start()` discovers `BaseDocument` subclasses in two passes: first
from every registered `MongoRepository._entity_type` (set by `__init_subclass__`),
then directly registered `BaseDocument` subclasses. This means defining a repository
is sufficient — you do not need to register document models separately.

Source files: `src/pyfly/data/document/auto_configuration.py`,
`src/pyfly/data/document/mongodb/initializer.py`.

---

## Testing

For unit tests, use [mongomock-motor](https://github.com/michaelkryukov/mongomock-motor)
or point at a dedicated test database:

::: listing pyfly-test.yaml | Listing B.11 — Test database configuration
pyfly:
  data:
    document:
      enabled: true
      database: "myapp_test"
:::

For integration tests, PyFly's Testcontainers support spins up a real MongoDB
container automatically — see the testing chapter and
`@ServiceConnection(MongoDBContainer)`.
