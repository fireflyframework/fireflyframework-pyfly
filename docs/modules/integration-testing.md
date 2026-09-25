# Integration Testing

PyFly's integration tests exercise adapters against **real backends** (Postgres, Redis, MongoDB,
Kafka, RabbitMQ, …) rather than mocks, so "it works" is provable. They live in `tests/integration/`,
are marked `@pytest.mark.integration` (auto-applied), and are **deselected from the default run**.

## Running

```bash
# Fast suite (default — no Docker, integration deselected):
uv run pytest

# Integration suite (needs Docker; testcontainers pulls images on first run):
uv run pytest -m integration tests/integration

# One backend lane, across the whole tree (what the CI integration-<lane> jobs run):
uv run pytest -m "integration and mysql" tests/

# Make missing backends FAIL instead of skip (what the CI integration jobs do):
PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration tests/integration
```

## How backends are provided

Each backend has a fixture. By default it starts a [testcontainer]; connection details are mapped
into pyfly config keys by `pyfly.testing.pyfly_config_for` (the Spring `@ServiceConnection`
equivalent). To reuse a long-lived local stack instead, run `docker compose up -d` and export the
matching `PYFLY_IT_*` env var — the fixture then uses your URL and starts no container:

| Fixture | Scope | Env override | docker-compose service |
|---|---|---|---|
| `pg_url` | one container per test | `PYFLY_IT_POSTGRES_URL` | postgres |
| `pg_server_url` | session, PostgreSQL 17 | `PYFLY_IT_POSTGRES_URL` | postgres |
| `mysql_url`, `mysql_server_url` | session, MySQL 8 | `PYFLY_IT_MYSQL_URL` | mysql |
| `mariadb_server_url` | session, MariaDB 11 | `PYFLY_IT_MARIADB_URL` | mariadb |
| `mongo_url` | session, standalone MongoDB 7 | `PYFLY_IT_MONGO_URI` | mongodb |
| `mongo_rs_url` | session, MongoDB 7 replica set `rs0` | `PYFLY_IT_MONGO_RS_URI` | mongodb-rs |
| `redis_url` | one container per test | `PYFLY_IT_REDIS_URL` | redis |
| `kafka_url` | session | `PYFLY_IT_KAFKA_BOOTSTRAP` | kafka |
| `amqp_url` | session | `PYFLY_IT_AMQP_URL` | rabbitmq |

The `*_server_url` fixtures point at an account that may create databases (the container superuser,
or root on MySQL/MariaDB), because the backend matrix creates a database per test on them. An override
URL must allow that too; the matrix switches its driver to the lane's (asyncpg, asyncmy).

## The backend matrix

`tests/support/backend_matrix.py` (a pytest plugin that `tests/conftest.py` registers) runs one test
body on every database the data layer supports. A test that requests `relational_backend` runs once per
lane, each time on a database of its own that is dropped afterwards:

| Lane | Backend | Marker | Runs in |
|---|---|---|---|
| `sqlite-file` | SQLite file in `tmp_path`, `PRAGMA foreign_keys=ON` | `sqlite_file` | the default suite |
| `pg` | PostgreSQL 17, asyncpg | `pg` + `integration` | `-m integration` |
| `mysql` | MySQL 8, asyncmy, pool pre-ping on | `mysql` + `integration` | `-m integration` |
| `mariadb` | MariaDB 11, asyncmy, pool pre-ping on | `mariadb` + `integration` | `-m integration` |

```python
import pytest
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import ContractChild, ContractParent


async def test_children_cascade(relational_backend: RelationalBackend) -> None:
    await relational_backend.create_tables(ContractParent, ContractChild)
    engine = relational_backend.create_engine()  # lane settings; disposed after the test
    ...


@pytest.mark.backends("sqlite-file", "pg")  # restrict the lanes (the default is all four)
async def test_pg_only(relational_backend: RelationalBackend) -> None: ...
```

`RelationalBackend` gives `lane`, `url`, `dialect`, `driver`, `create_engine(**options)`,
`create_tables(*models)`, `with_driver("aiomysql")` (the same database through another driver) and
`config(overrides)`, a PyFly `Config` with that database as the primary datasource. `config()` sets
`ddl-auto: none`: create the tables a test needs with `create_tables`, because `Base.metadata` holds
every model imported in the run. The lane's settings (SQLite foreign keys, pre-ping) apply to engines
from `create_engine()`; an engine the application builds from `config()` gets the framework's own
settings (MySQL/MariaDB `config()` does set `pool.pre-ping`), so a test of the framework is not
flattered by the harness. `mongo_backend` is the document counterpart: a fresh database on the
replica set, with `url`, `database` and `config()`.

The lanes deliberately run the settings that broke before: foreign keys are enforced on SQLite, and
pool pre-ping is on for MySQL and MariaDB. `tests/support/contract_models.py` holds the models the
repository contract tests share: a parent/child pair with a cascading relationship, a versioned
entity, a composite key and a soft-delete entity, all portable to every lane.

Markers decide what runs where:

- A server lane carries `integration` plus its lane marker (`pg`, `mysql`, `mariadb`). So does any
  test that uses a server fixture directly, wherever it lives (`mongo` for the Mongo fixtures, `brokers`
  for Redis, Kafka and RabbitMQ). `-m "integration and mariadb"` runs one lane. Only the fixtures of
  the backend-matrix plugin and of `tests/integration/conftest.py` count: a unit test with a local
  fixture that happens to be called `redis_url` stays in the default suite.
- Under `tests/integration/`, the directory marker is not added to the `sqlite-file` runs. A matrix
  test placed there runs SQLite in the default suite and the servers in the integration suite.

To count what a test sends, use `pyfly.testing.StatementCounter` (see the
[Testing Guide](testing.md#statementcounter-sql-statements-per-operation)).

## Skip vs. fail

`pyfly.testing.is_docker_available()` / `@requires_docker` skip a test when no Docker daemon
answers, so the suite degrades cleanly on machines without Docker. Setting
`PYFLY_INTEGRATION_REQUIRE_DOCKER=1` flips every skip into a hard failure and aborts the run if the
daemon is unreachable — this is set only in the dedicated CI integration jobs (manual dispatch +
nightly). Those jobs are **not** PR merge gates; the fast unit suite, which includes every
`sqlite-file` run, remains the gate on every push.

## CI jobs

| Job | Command | Covers |
|---|---|---|
| `test` | `pytest tests/` | the fast suite, including the `sqlite-file` lane |
| `integration` | `pytest -m integration tests/integration` | everything under `tests/integration/`, brokers included |
| `integration-lanes` (`integration-pg`, `integration-mysql`, `integration-mariadb`, `integration-mongo-rs`) | `pytest -m "integration and <lane>" tests/` | one database lane across the whole tree |

## Writing a new integration test

```python
import pytest
from pyfly.testing import requires_docker

@requires_docker
@pytest.mark.asyncio
async def test_my_adapter(pg_url: str) -> None:
    # build the real adapter against pg_url and assert real-backend behavior
    ...
```

Place it under `tests/integration/` (the `integration` marker is applied automatically). Namespace
your data (unique keys/topics/tables) because session-scoped containers are shared across the run,
or use `relational_backend` / `mongo_backend`, which give each test a database of its own.

[testcontainer]: https://testcontainers.com/
