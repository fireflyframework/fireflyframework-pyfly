# SQLAlchemy Adapter

> **Module:** Data Relational — [Module Guide](../modules/data-relational.md)
> **Package:** `pyfly.data.relational.sqlalchemy`
> **Backend:** SQLAlchemy 2.0+ (async), Alembic, aiosqlite

## Quick Start

### Installation

```bash
uv add "pyfly[data-relational]"

# For PostgreSQL (production)
uv add "pyfly[data-relational,postgresql]"
```

### Minimal Configuration

```yaml
# pyfly.yaml
pyfly:
  data:
    relational:
      enabled: true
      url: "sqlite+aiosqlite:///app.db"
```

### Minimal Example

```python
from pyfly.container import repository
from pyfly.data.relational.sqlalchemy import Repository, BaseEntity

class OrderEntity(BaseEntity):
    __tablename__ = "orders"
    name: str
    total: float

@repository
class OrderRepository(Repository[OrderEntity, int]):
    async def find_by_name(self, name: str) -> list[OrderEntity]: ...
```

---

## Configuration Reference

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pyfly.data.relational.enabled` | `bool` | `false` | Enable the SQLAlchemy adapter |
| `pyfly.data.relational.url` | `str` | `"sqlite+aiosqlite:///pyfly.db"` | Database connection URL |
| `pyfly.data.relational.echo` | `bool` | `false` | Log all SQL statements |
| `pyfly.data.relational.ddl-auto` | `str` | `"create"` | DDL strategy: `create`, `create-drop`, or `none` |
| `pyfly.data.relational.pool.size` | `int` | *(driver default)* | Connection pool size (`pool_size`) |
| `pyfly.data.relational.pool.max-overflow` | `int` | *(driver default)* | Max overflow connections above pool size |
| `pyfly.data.relational.pool.timeout` | `float` | *(driver default)* | Seconds to wait for a connection from the pool |
| `pyfly.data.relational.pool.recycle` | `int` | *(driver default)* | Seconds before a connection is recycled |
| `pyfly.data.relational.pool.pre-ping` | `bool` | *(driver default)* | Issue a `SELECT 1` ping before each checkout |

### Database URLs by Driver

| Database | URL Format |
|----------|-----------|
| SQLite | `sqlite+aiosqlite:///app.db` |
| PostgreSQL | `postgresql+asyncpg://user:pass@host:5432/db` |
| MySQL | `mysql+aiomysql://user:pass@host:3306/db` |

---

## Adapter-Specific Features

### BaseEntity

`BaseEntity` provides audit fields automatically:

- `id` — Auto-generated primary key
- `created_at` — Timestamp set on insert
- `updated_at` — Timestamp updated on modification
- `created_by` / `updated_by` — Audit user tracking

### RepositoryBeanPostProcessor

The `RepositoryBeanPostProcessor` wires derived query methods (e.g., `find_by_name_and_active`) onto `Repository` subclasses at startup. No implementation needed — just define the method signature.

### QueryMethodCompiler

Compiles derived query method names into SQLAlchemy queries using the `QueryMethodParser` from the shared data commons layer.

### Specification Pattern

Build dynamic queries with `Specification[T]`:

```python
spec = (
    Specification.where(field="status", op="eq", value="ACTIVE")
    .and_where(field="total", op="gt", value=100)
)
results = await repository.find_all_by_spec(spec)
```

### Alembic Migrations

```bash
pyfly db init          # Initialize Alembic
pyfly db migrate -m "add orders table"
pyfly db upgrade       # Apply pending migrations
```

---

## Testing

Use SQLite in-memory for tests:

```yaml
# pyfly-test.yaml
pyfly:
  data:
    relational:
      url: "sqlite+aiosqlite:///:memory:"
```

---

## See Also

- [Data Commons Guide](../modules/data.md) — Shared port APIs: `RepositoryPort`, derived query parsing, `Page`/`Pageable`/`Sort`, `Mapper`
- [Data Relational Module Guide](../modules/data-relational.md) — SQLAlchemy adapter: repositories, specifications, transactions, custom queries
- [Adapter Catalog](README.md)
