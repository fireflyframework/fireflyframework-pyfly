# MongoDB Adapter

> **Module:** Data Document — [Module Guide](../modules/data-document.md)
> **Package:** `pyfly.data.document.mongodb`
> **Backend:** Motor 3.3+, Beanie 1.25+ (ODM)

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
| `pyfly.data.document.min_pool_size` | `int` | `0` | Minimum connection pool size |
| `pyfly.data.document.max_pool_size` | `int` | `100` | Maximum connection pool size |

---

## Adapter-Specific Features

### BaseDocument

`BaseDocument` extends Beanie's `Document` with audit fields:

- `created_at` — Timestamp set on insert
- `updated_at` — Timestamp updated on modification
- `created_by` / `updated_by` — Audit user tracking

### Beanie Initialization

The adapter calls `init_beanie()` at startup to register all document models with the Motor client. Document discovery is automatic via the DI container.

### MongoQueryMethodCompiler

Compiles derived query method names (e.g., `find_by_status_and_name`) into MongoDB queries using Beanie's find operators. Shares the same `QueryMethodParser` as the relational adapter.

### MongoRepositoryBeanPostProcessor

Wires compiled query methods onto `MongoRepository` subclasses at startup — identical behavior to the SQLAlchemy `RepositoryBeanPostProcessor`.

### Transactions

Use `@mongo_transactional` for multi-document transactions. The decorator
requires a `Motor` client (injected from the auto-configured bean):

```python
from pyfly.data.document.mongodb import mongo_transactional
from motor.motor_asyncio import AsyncIOMotorClient

client: AsyncIOMotorClient = ...  # injected by DI

@mongo_transactional(client)
async def transfer(from_id: str, to_id: str, amount: float) -> None:
    ...
```

---

## Testing

Use a test MongoDB instance or [mongomock-motor](https://github.com/michaelkryukov/mongomock-motor) for unit tests. Configure a dedicated test database:

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
