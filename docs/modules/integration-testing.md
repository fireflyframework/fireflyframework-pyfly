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

# Make missing backends FAIL instead of skip (what the CI integration job does):
PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration tests/integration
```

## How backends are provided

Each backend has a fixture (`pg_url`, `redis_url`, `mysql_url`, `mongo_url`, `kafka_url`,
`amqp_url`). By default it starts a [testcontainer]; connection details are mapped into pyfly
config keys by `pyfly.testing.pyfly_config_for` (the Spring `@ServiceConnection` equivalent). To
reuse a long-lived local stack instead, run `docker compose up -d` and export the matching
`PYFLY_IT_*` env var — the fixture then uses your URL and starts no container:

| Fixture | Env override | docker-compose service |
|---|---|---|
| `pg_url` | `PYFLY_IT_POSTGRES_URL` | postgres |
| `redis_url` | `PYFLY_IT_REDIS_URL` | redis |
| `mysql_url` | `PYFLY_IT_MYSQL_URL` | mysql |
| `mongo_url` | `PYFLY_IT_MONGO_URI` | mongodb |
| `kafka_url` | `PYFLY_IT_KAFKA_BOOTSTRAP` | kafka |
| `amqp_url` | `PYFLY_IT_AMQP_URL` | rabbitmq |

## Skip vs. fail

`pyfly.testing.is_docker_available()` / `@requires_docker` skip a test when no Docker daemon
answers, so the suite degrades cleanly on machines without Docker. Setting
`PYFLY_INTEGRATION_REQUIRE_DOCKER=1` flips every skip into a hard failure and aborts the run if the
daemon is unreachable — this is set only in the dedicated CI `integration` job (manual dispatch +
nightly). That job is **not** a PR merge gate; the fast unit suite remains the gate on every push.

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
your data (unique keys/topics/tables) because session-scoped containers are shared across the run.

[testcontainer]: https://testcontainers.com/
