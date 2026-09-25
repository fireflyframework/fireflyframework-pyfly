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
real Postgres/MySQL/MariaDB/Redis/MongoDB/Kafka in Docker, then wire its connection details
straight into pyfly config keys via :func:`pyfly_config_for` / :func:`pyfly_config`.
:func:`mongodb_replica_set_container` starts the single-node replica set that MongoDB needs for
multi-document transactions.

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
import importlib.util
import time
from collections.abc import Callable
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

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


def mariadb_container(image: str = "mariadb:11", **kwargs: Any) -> Any:
    """A ``testcontainers`` MySqlContainer running MariaDB (the image accepts the MySQL variables).

    :func:`pyfly_config_for` recognizes the MariaDB image and maps it to a ``mariadb+`` URL.
    """
    return _load("testcontainers.mysql", "MySqlContainer")(image, **kwargs)


def redis_container(image: str = "redis:7-alpine", **kwargs: Any) -> Any:
    """A ``testcontainers`` RedisContainer."""
    return _load("testcontainers.redis", "RedisContainer")(image, **kwargs)


def mongodb_container(image: str = "mongo:7", **kwargs: Any) -> Any:
    """A ``testcontainers`` MongoDbContainer (a standalone server: no multi-document transactions)."""
    return _load("testcontainers.mongodb", "MongoDbContainer")(image, **kwargs)


_ALREADY_INITIALIZED = 23  # MongoDB error code AlreadyInitialized: replSetInitiate on an initiated set


class MongoDbReplicaSetContainer:
    """A MongoDB single-node replica set in Docker, the smallest topology that runs transactions.

    A standalone ``mongod`` rejects ``startTransaction``, so every test of a Mongo transaction needs a
    replica set. :meth:`start` runs ``mongod --replSet <name> --bind_ip_all``, initiates the set with
    itself as the only member (``rs.initiate()``), and returns once that member is a writable primary.

    :meth:`get_connection_url` carries ``directConnection=true``: the client talks to this one member
    and skips replica-set discovery, so the member's advertised ``localhost`` host never has to
    resolve from the test process. The server runs without authentication.

    Use it like any testcontainer (``with mongodb_replica_set_container() as mongo: ...``);
    :func:`pyfly_config_for` maps it to ``pyfly.data.document.uri``. Starting it needs ``pymongo``,
    which ``pyfly[data-document]`` installs.
    """

    def __init__(
        self,
        image: str = "mongo:7",
        *,
        replica_set: str = "rs0",
        port: int = 27017,
        startup_timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        self.image = image
        self.replica_set = replica_set
        self.port = port
        self.startup_timeout = startup_timeout
        container_cls = _load("testcontainers.core.container", "DockerContainer")
        self._container: Any = (
            container_cls(image, **kwargs)
            .with_command(["--replSet", replica_set, "--bind_ip_all", "--port", str(port)])
            .with_exposed_ports(port)
        )

    def start(self) -> Self:
        """Start ``mongod``, initiate the replica set and wait for a writable primary."""
        self._container.start()
        try:
            self._initiate()
        except BaseException:
            self._container.stop()
            raise
        return self

    def stop(self) -> None:
        """Stop and remove the container."""
        self._container.stop()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    def get_container_host_ip(self) -> str:
        """The host the mapped port is published on."""
        return str(self._container.get_container_host_ip())

    def get_exposed_port(self, port: int) -> int:
        """The host port *port* inside the container is published on."""
        return int(self._container.get_exposed_port(port))

    def get_connection_url(self) -> str:
        """A ``mongodb://`` URL for the member, with ``directConnection=true``."""
        return f"mongodb://{self.get_container_host_ip()}:{self.get_exposed_port(self.port)}/?directConnection=true"

    def get_wrapped_container(self) -> Any:
        """The underlying ``testcontainers`` ``DockerContainer``."""
        return self._container

    def _initiate(self) -> None:
        try:
            pymongo = importlib.import_module("pymongo")
            errors = importlib.import_module("pymongo.errors")
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "The MongoDB replica-set container needs pymongo: pip install 'pyfly[data-document]'."
            ) from exc
        deadline = time.monotonic() + self.startup_timeout
        client = pymongo.MongoClient(self.get_connection_url(), serverSelectionTimeoutMS=1_000)
        try:
            self._await(deadline, lambda: client.admin.command("ping"), errors.PyMongoError, "accept connections")
            member = {"_id": 0, "host": f"localhost:{self.port}"}
            try:
                client.admin.command("replSetInitiate", {"_id": self.replica_set, "members": [member]})
            except errors.OperationFailure as exc:
                if exc.code != _ALREADY_INITIALIZED:
                    raise

            def writable_primary() -> None:
                if not client.admin.command("hello").get("isWritablePrimary"):
                    raise errors.PyMongoError("not yet a writable primary")

            self._await(deadline, writable_primary, errors.PyMongoError, "become a writable primary")
        finally:
            client.close()

    def _await(self, deadline: float, probe: Callable[[], object], retry_on: type[BaseException], what: str) -> None:
        while True:
            try:
                probe()
                return
            except retry_on as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"MongoDB replica set {self.replica_set!r} did not {what} within {self.startup_timeout:g}s"
                    ) from exc
                time.sleep(0.25)


def mongodb_replica_set_container(image: str = "mongo:7", **kwargs: Any) -> MongoDbReplicaSetContainer:
    """A :class:`MongoDbReplicaSetContainer`: single-node replica set ``rs0``, ready for transactions."""
    return MongoDbReplicaSetContainer(image, **kwargs)


def kafka_container(image: str = "confluentinc/cp-kafka:7.6.0", **kwargs: Any) -> Any:
    """A ``testcontainers`` KafkaContainer."""
    return _load("testcontainers.kafka", "KafkaContainer")(image, **kwargs)


def rabbitmq_container(image: str = "rabbitmq:3.13-alpine", **kwargs: Any) -> Any:
    """A ``testcontainers`` RabbitMqContainer."""
    return _load("testcontainers.rabbitmq", "RabbitMqContainer")(image, **kwargs)


_MYSQL_ASYNC_DRIVERS = ("asyncmy", "aiomysql")


def _mysql_async_driver() -> str:
    """The async driver to put in a rewritten MySQL/MariaDB URL.

    asyncmy (installed by the ``mysql`` extra) when it is importable; aiomysql when that is the one
    installed; asyncmy otherwise, since that is what ``pip install 'pyfly[mysql]'`` provides.
    """
    for driver in _MYSQL_ASYNC_DRIVERS:
        if importlib.util.find_spec(driver) is not None:
            return driver
    return _MYSQL_ASYNC_DRIVERS[0]


def _async_db_url(url: str, replacements: dict[str, str]) -> str:
    for sync_prefix, async_prefix in replacements.items():
        if url.startswith(sync_prefix):
            return async_prefix + url[len(sync_prefix) :]
    return url


def pyfly_config_for(container: Any) -> dict[str, Any]:
    """Map a **started** testcontainer to pyfly config overrides (the ``@ServiceConnection``
    equivalent). Returns flat dotted keys; raises ``ValueError`` for unmapped types.

    Database URLs are rewritten to PyFly's async drivers: ``postgresql+asyncpg://`` for Postgres, and
    ``mysql+asyncmy://`` (or ``mariadb+asyncmy://`` for a MariaDB image) for MySQL containers. The
    MySQL driver is asyncmy, which the ``mysql`` extra installs; when only aiomysql is installed, the
    URL uses aiomysql instead.
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
        backend = "mariadb" if "mariadb" in str(getattr(container, "image", "")).lower() else "mysql"
        async_prefix = f"{backend}+{_mysql_async_driver()}://"
        url = _async_db_url(
            container.get_connection_url(),
            {"mysql+pymysql://": async_prefix, "mysql://": async_prefix},
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
    if "RabbitMq" in name or "RabbitMQ" in name:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5672)
        url = f"amqp://guest:guest@{host}:{port}/"
        return {"pyfly.eda.rabbitmq.url": url, "pyfly.messaging.rabbitmq.url": url}
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
