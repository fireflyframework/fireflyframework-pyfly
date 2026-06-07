# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Testcontainers — Docker-backed integration-test fixtures.

The Spring Boot ``@Testcontainers`` / ``@ServiceConnection`` equivalent: spin up a
real Postgres/MySQL/Redis/MongoDB/Kafka in Docker, then wire its connection details
straight into pyfly config keys via :func:`pyfly_config_for` / :func:`pyfly_config`.

Requires the extra and a running Docker daemon::

    pip install 'pyfly[testcontainers]'

Guard integration tests so they skip cleanly where Docker is unavailable::

    from pyfly.testing.testcontainers import postgres_container, pyfly_config, requires_docker

    @requires_docker
    def test_with_real_postgres():
        with postgres_container() as pg:
            config = pyfly_config(pg)            # -> pyfly.data.relational.url = the container
            ...
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, TypeVar, cast

if TYPE_CHECKING:
    from pyfly.core.config import Config

F = TypeVar("F")

_EXTRA_HINT = (
    "Testcontainers support requires the extra and a running Docker daemon: pip install 'pyfly[testcontainers]'."
)
_NO_DOCKER = "Docker is not available (daemon down or the pyfly[testcontainers] extra is not installed)."


def is_docker_available() -> bool:
    """Whether a Docker daemon is reachable — integration tests should skip if not."""
    try:
        import docker  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        return False
    try:
        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001 - any connectivity failure means "not available"
        return False


def requires_docker(func: F) -> F:
    """``pytest`` decorator that skips the test when Docker is unavailable."""
    import pytest

    return cast(F, pytest.mark.skipif(not is_docker_available(), reason=_NO_DOCKER)(func))


def _load(module: str, name: str) -> Any:
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(_EXTRA_HINT) from exc
    return getattr(loaded, name)


def postgres_container(image: str = "postgres:16-alpine", **kwargs: Any) -> Any:
    """A ``testcontainers`` PostgresContainer (start via ``with``)."""
    return _load("testcontainers.postgres", "PostgresContainer")(image, **kwargs)


def mysql_container(image: str = "mysql:8", **kwargs: Any) -> Any:
    """A ``testcontainers`` MySqlContainer."""
    return _load("testcontainers.mysql", "MySqlContainer")(image, **kwargs)


def redis_container(image: str = "redis:7-alpine", **kwargs: Any) -> Any:
    """A ``testcontainers`` RedisContainer."""
    return _load("testcontainers.redis", "RedisContainer")(image, **kwargs)


def mongodb_container(image: str = "mongo:7", **kwargs: Any) -> Any:
    """A ``testcontainers`` MongoDbContainer."""
    return _load("testcontainers.mongodb", "MongoDbContainer")(image, **kwargs)


def kafka_container(image: str = "confluentinc/cp-kafka:7.6.0", **kwargs: Any) -> Any:
    """A ``testcontainers`` KafkaContainer."""
    return _load("testcontainers.kafka", "KafkaContainer")(image, **kwargs)


def _async_db_url(url: str, replacements: dict[str, str]) -> str:
    for sync_prefix, async_prefix in replacements.items():
        if url.startswith(sync_prefix):
            return async_prefix + url[len(sync_prefix) :]
    return url


def pyfly_config_for(container: Any) -> dict[str, Any]:
    """Map a **started** testcontainer to pyfly config overrides (the ``@ServiceConnection``
    equivalent). Returns flat dotted keys; raises ``ValueError`` for unmapped types.
    """
    name = type(container).__name__
    if "Postgres" in name:
        url = _async_db_url(
            container.get_connection_url(),
            {
                "postgresql+psycopg2://": "postgresql+asyncpg://",
                "postgresql+psycopg://": "postgresql+asyncpg://",
                "postgresql://": "postgresql+asyncpg://",
            },
        )
        return {"pyfly.data.relational.url": url}
    if "MySql" in name or "MySQL" in name:
        url = _async_db_url(
            container.get_connection_url(),
            {"mysql+pymysql://": "mysql+aiomysql://", "mysql://": "mysql+aiomysql://"},
        )
        return {"pyfly.data.relational.url": url}
    if "Redis" in name:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        return {"pyfly.cache.redis.url": url, "pyfly.session.redis.url": url}
    if "Mongo" in name:
        return {"pyfly.data.document.uri": container.get_connection_url()}
    if "Kafka" in name:
        return {"pyfly.eda.kafka.bootstrap-servers": container.get_bootstrap_server()}
    raise ValueError(f"No pyfly config mapping for container type {name!r}")


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn flat dotted keys into a nested dict (``{'a.b': 1}`` -> ``{'a': {'b': 1}}``)."""
    root: dict[str, Any] = {}
    for dotted, value in flat.items():
        node = root
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return root


def pyfly_config(*containers: Any, base: dict[str, Any] | None = None) -> Config:
    """Build a pyfly ``Config`` wiring every started container's connection details.

    Merges :func:`pyfly_config_for` for each container (plus optional *base* flat
    overrides) into a nested config — one-call setup for an integration ApplicationContext.
    """
    from pyfly.core.config import Config

    merged: dict[str, Any] = dict(base or {})
    for container in containers:
        merged.update(pyfly_config_for(container))
    return Config(_nest(merged))
