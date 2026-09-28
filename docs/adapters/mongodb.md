# MongoDB Adapter

> **Module:** Data Document — [Module Guide](../modules/data-document.md)
> **Package:** `pyfly.data.document.mongodb`
> **Backend:** pymongo 4.x async client (`AsyncMongoClient`), Beanie 2.1+ (ODM)

## Quick Start

### Installation

```bash
uv add "pyfly[data-document]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  data:
    document:
      enabled: true
      uri: "mongodb://localhost:27017"
      database: "myapp"
```

### Minimal Example

```python
from pyfly.container import repository
from pyfly.data.document.mongodb import MongoRepository, BaseDocument

class OrderDocument(BaseDocument):
    name: str
    total: float

@repository
class OrderRepository(MongoRepository[OrderDocument, str]):
    async def find_by_name(self, name: str) -> list[OrderDocument]: ...
```

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.data.document.enabled` | `bool` | `false` | Enable the MongoDB adapter |
| `pyfly.data.document.uri` | `str` | `"mongodb://localhost:27017"` | MongoDB connection URI |
| `pyfly.data.document.database` | `str` | `"pyfly"` | Database name |
| `pyfly.data.document.datasource` | `str` | `"document"` | Datasource name of the document units of work |
| `pyfly.data.document.min_pool_size` | `int` | `0` | Minimum connection pool size |
| `pyfly.data.document.max_pool_size` | `int` | `100` | Maximum connection pool size |
| `pyfly.data.document.max-idle-time`, `connect-timeout`, `server-selection-timeout`, `socket-timeout`, `wait-queue-timeout` | `float` (seconds) | pymongo's | Pool and connection timeouts |
| `pyfly.data.document.app-name` | `str` | none | Application name the server logs |
| `pyfly.data.document.tz-aware` | `bool` | `true` | Datetimes come back as aware UTC values |
| `pyfly.data.document.uuid-representation` | `str` | `"standard"` | How UUIDs are stored |
| `pyfly.data.document.options` | map | `{}` | Any other `AsyncMongoClient` keyword argument |
| `pyfly.data.document.models` | list | `[]` | Document classes or packages to initialize besides the repositories' |
| `pyfly.data.document.transaction.read-concern` / `write-concern` / `max-commit-time` | | the client's | Transaction options |
| `pyfly.data.document.transaction.default` | `bool` | when no relational layer | Whether the document datasource is `@transactional`'s default |
| `pyfly.data.document.health.timeout` | `float` | `2.0` | Seconds the readiness check waits for `ping` |

---

## Adapter-Specific Features

### BaseDocument

`BaseDocument` extends Beanie's `Document` with audit fields, kept current on every write with the application's `AuditorAware` and `DateTimeProvider`:

- `created_at` — set on insert (aware UTC)
- `updated_at` — set on insert and every update (aware UTC)
- `created_by` / `updated_by` — the auditor on insert, and the current auditor on every update

`AggregateDocument` adds `raise_event()`: domain events published when the unit of work that saves the document commits.

### Beanie Initialization

The adapter calls `init_beanie()` at startup to bind the document models to the database: every repository's document, the documents their `Link` fields reach and the configured `models`. Beanie binds a class to one database per process: contexts that configure the same client share it, and a context that would rebind the documents to another database fails to start (one document datasource per process).

### MongoQueryMethodCompiler

Compiles derived query method names (e.g., `find_by_status_and_name`) into MongoDB queries using Beanie's find operators. Shares the same `QueryMethodParser` as the relational adapter.

### MongoRepositoryBeanPostProcessor

Wires compiled query methods onto `MongoRepository` subclasses at startup — identical behavior to the SQLAlchemy `RepositoryBeanPostProcessor`.

### Transactions

Use the unified **`@transactional`** (from `pyfly.data`) for multi-document transactions: the same annotation, and
the same unit-of-work semantics, as the relational backend. The `MongoTransactionManager` binds the unit's
`ClientSession` to the running task, and every `MongoRepository` call inside the unit passes it to the driver
(requires a MongoDB replica set; a standalone server refuses a transaction with a clear error):

```python
from pyfly.container import service
from pyfly.data import transactional


@service
class AccountService:
    def __init__(self, accounts: AccountRepository) -> None:
        self._accounts = accounts

    @transactional(datasource="document")
    async def transfer(self, from_id: str, to_id: str, amount: float) -> None:
        source = await self._accounts.find_by_id(from_id)
        target = await self._accounts.find_by_id(to_id)
        source.balance -= amount
        target.balance += amount
        await self._accounts.save_all([source, target])
```

Every propagation but `NESTED` (MongoDB has no savepoints) works as on the relational backend. A legacy service
exposing `self._motor_client` (an `AsyncMongoClient`) still selects the MongoDB manager, and a coroutine that
declares a `session` parameter still receives the unit's session; `current_session()` gives it to code that calls
Beanie directly. Outside a transaction, repository reads run without one and writes in a short unit of their own.

> `from pyfly.data.document.mongodb import mongo_transactional` still works but is a **deprecated
> alias** of `@transactional`.

---

## Testing

Test against a real MongoDB replica set: `pyfly.testing.testcontainers.mongodb_replica_set_container()` starts a
single-node one (MongoDB 7, `rs0`, `directConnection=true`) that runs transactions, and
`pyfly_config_for(container)` points `pyfly.data.document.uri` at it. The repositories pass a session on every
call, which mongomock does not support. Configure a dedicated test database:

```yaml
# pyfly-test.yaml
pyfly:
  data:
    document:
      database: "myapp_test"
```

---

## See Also

- [Data Commons Guide](../modules/data.md) — Shared port APIs: `RepositoryPort`, derived query parsing, `Page`/`Pageable`/`Sort`, `Mapper`
- [Data Document Module Guide](../modules/data-document.md) — MongoDB adapter: MongoRepository, derived queries, Beanie ODM patterns
- [Adapter Catalog](README.md)
