# SQLAlchemy Adapter

> **Module:** Data Relational — [Module Guide](../modules/data-relational.md)
> **Package:** `pyfly.data.relational.sqlalchemy`
> **Backend:** SQLAlchemy 2.0.50+ (async), Alembic, aiosqlite

## Quick Start

### Installation

```bash
uv add "pyfly[data-relational]"

# For PostgreSQL (production)
uv add "pyfly[data-relational,postgresql]"

# For MySQL or MariaDB (asyncmy driver)
uv add "pyfly[data-relational,mysql]"
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
| `pyfly.data.relational.url` | `str` | *(required)* | Database connection URL. Startup fails without it, except in the `dev` profile (`sqlite+aiosqlite:///./app.db`, with a warning) |
| `pyfly.data.relational.echo` | `bool` or `debug` | `false` | Log all SQL statements (`debug` also logs rows); `"false"` from an env var is `false` |
| `pyfly.data.relational.ddl-auto` | `str` | `"create"` | DDL strategy: `create`, `create-drop`, or `none` |
| `pyfly.data.relational.pool.size` | `int` | *(driver default)* | Connection pool size (`pool_size`) |
| `pyfly.data.relational.pool.max-overflow` | `int` | *(driver default)* | Max overflow connections above pool size |
| `pyfly.data.relational.pool.timeout` | `float` | *(driver default)* | Seconds to wait for a connection from the pool |
| `pyfly.data.relational.pool.recycle` | `int` | `1800` | Seconds before a connection is recycled (`-1` never) |
| `pyfly.data.relational.pool.pre-ping` | `bool` | `false` | Test each pooled connection at checkout (the driver's ping on MySQL/MariaDB) and replace it if the server dropped it |
| `pyfly.data.relational.connect-args.*` | mapping | — | Passed to the driver verbatim (asyncpg `statement_cache_size: 0` behind pgbouncer, `server_settings`, SSL, timeouts) |
| `pyfly.data.relational.sqlite.*` | mapping | see below | `foreign-keys` (`true`), `journal-mode` (`WAL`), `synchronous` (`NORMAL`), `busy-timeout` (`5000` ms) |
| `pyfly.data.relational.health.timeout` | `float` | `2` | Seconds each `db` readiness check may take |

Every key accepts `${...}` placeholders and `PYFLY_*` overrides, and the same settings apply to the
read replica, the named datasources and the datasources the framework modules use. All of them are
built by one datasource registry; see
[Datasource Registry](../modules/data-relational.md#datasource-registry).

### Database URLs by Driver

| Database | URL Format |
|----------|-----------|
| SQLite | `sqlite+aiosqlite:///app.db` |
| PostgreSQL | `postgresql+asyncpg://user:pass@host:5432/db` |
| MySQL | `mysql+asyncmy://user:pass@host:3306/db` (`pyfly[mysql]`; `mysql+aiomysql://` also works) |
| MariaDB | `mariadb+asyncmy://user:pass@host:3306/db` (`pyfly[mysql]`; `mariadb+aiomysql://` also works) |

SQLite, PostgreSQL, MySQL 8 and MariaDB 11 are the databases the test suite runs on. On MySQL and
MariaDB, `pool.pre-ping` needs SQLAlchemy 2.0.50 or later, which the `data-relational` extra requires:
with 2.0.49 and PyMySQL 1.2 installed, every other pre-pinged checkout raised `TypeError`.

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
