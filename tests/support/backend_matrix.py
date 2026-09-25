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
"""Backend matrix: one test body, every database the data layer supports.

A test that requests the ``relational_backend`` fixture runs once per relational lane, and each run
gets an isolated database of its own:

======================  ==================================================================  ===========
Lane                    Backend                                                             Runs in
======================  ==================================================================  ===========
``sqlite-file``         SQLite on a file in the test's ``tmp_path``, foreign keys ON        fast suite
``pg``                  PostgreSQL 17 (asyncpg), a fresh database per test                  integration
``mysql``               MySQL 8 (asyncmy), a fresh database per test, pool pre-ping ON      integration
``mariadb``             MariaDB 11 (asyncmy), a fresh database per test, pool pre-ping ON   integration
======================  ==================================================================  ===========

The document lane is separate: ``mongo_rs_url`` is a MongoDB 7 single-node replica set (``rs0``,
``directConnection=true``), the only topology that runs multi-document transactions, and
``mongo_backend`` gives a test a fresh database on it.

Usage::

    from tests.support.backend_matrix import RelationalBackend

    async def test_round_trip(relational_backend: RelationalBackend) -> None:
        await relational_backend.create_tables(Order)
        engine = relational_backend.create_engine()
        ...

    @pytest.mark.backends("sqlite-file", "pg")   # restrict the lanes; default is all four
    async def test_something_pg_specific(relational_backend: RelationalBackend) -> None: ...

The sqlite-file run has no ``integration`` marker, so the default ``pytest`` run executes it. Each
server run carries ``integration`` plus its lane marker (``pg``, ``mysql``, ``mariadb``), so
``pytest -m integration`` runs every server lane and ``pytest -m "integration and mysql"`` runs one.
Tests that use a server fixture directly (``pg_url``, ``mongo_rs_url``, ``redis_url``, ...) get the
same two markers from :func:`pytest_collection_modifyitems`, wherever they live. Under
``tests/integration/`` the directory marker is not added to the sqlite-file runs, so a matrix test
placed there runs SQLite in the fast suite and the servers in the integration suite.

Servers are started once per test session through testcontainers, or taken from the ``PYFLY_IT_*``
environment variables (``docker compose up``, see ``docker-compose.yml``), and each test gets its own
database, dropped afterwards. The lanes deliberately exercise the settings that broke in production:
foreign keys are enforced on SQLite (C161) and pool pre-ping is on for MySQL/MariaDB (C089).

This module is registered as a pytest plugin by ``tests/conftest.py``. Its plain helpers
(:func:`start_server`, :func:`create_database`, :func:`drop_database`, :class:`RelationalBackend`)
carry no pytest state, so the data-layer benchmarks (``benchmarks/data/``) reuse them.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import pytest
from sqlalchemy import Table, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.core.config import Config
from pyfly.testing.testcontainers import (
    mariadb_container,
    mongodb_replica_set_container,
    mysql_container,
    postgres_container,
    pyfly_config,
    pyfly_config_for,
)

# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------

SQLITE_FILE = "sqlite-file"
PG = "pg"
MYSQL = "mysql"
MARIADB = "mariadb"
MONGO = "mongo"

RELATIONAL_LANES: tuple[str, ...] = (SQLITE_FILE, PG, MYSQL, MARIADB)
"""Every relational lane, in the order a parametrized test lists them."""

SERVER_LANES: tuple[str, ...] = (PG, MYSQL, MARIADB)
"""The relational lanes that need a database server (and therefore Docker or a ``PYFLY_IT_*`` URL)."""

LANE_MARKERS: dict[str, str] = {
    SQLITE_FILE: "sqlite_file",
    PG: "pg",
    MYSQL: "mysql",
    MARIADB: "mariadb",
    MONGO: "mongo",
}
"""Lane -> the pytest marker that selects it (``pytest -m "integration and mysql"``)."""

FIXTURE_LANES: dict[str, str] = {
    "pg_url": PG,
    "pg_server_url": PG,
    "mysql_url": MYSQL,
    "mysql_server_url": MYSQL,
    "mariadb_server_url": MARIADB,
    "mongo_url": MONGO,
    "mongo_rs_url": MONGO,
    "mongo_backend": MONGO,
    "redis_url": "brokers",
    "kafka_url": "brokers",
    "amqp_url": "brokers",
}
"""Server fixtures -> the lane marker every test that uses them gets, together with ``integration``."""

MYSQL_DRIVERS: tuple[str, ...] = ("asyncmy", "aiomysql")
"""The MySQL/MariaDB async drivers the lanes prove: asyncmy (the ``mysql`` extra) and aiomysql."""


@dataclass(frozen=True)
class ServerSpec:
    """How a server lane is provided: the image, the override variable and the lane's async driver."""

    lane: str
    title: str
    image: str
    env_var: str
    drivername: str


SERVER_SPECS: dict[str, ServerSpec] = {
    PG: ServerSpec(PG, "PostgreSQL", "postgres:17-alpine", "PYFLY_IT_POSTGRES_URL", "postgresql+asyncpg"),
    MYSQL: ServerSpec(MYSQL, "MySQL", "mysql:8", "PYFLY_IT_MYSQL_URL", "mysql+asyncmy"),
    MARIADB: ServerSpec(MARIADB, "MariaDB", "mariadb:11", "PYFLY_IT_MARIADB_URL", "mariadb+asyncmy"),
}

MONGO_IMAGE = "mongo:7"
MONGO_ENV_VAR = "PYFLY_IT_MONGO_RS_URI"

_DATABASE_PREFIX = "pyfly_t_"


def unavailable(reason: str) -> NoReturn:
    """Skip the test, or fail it when ``PYFLY_INTEGRATION_REQUIRE_DOCKER=1`` (the CI lanes set it)."""
    if os.environ.get("PYFLY_INTEGRATION_REQUIRE_DOCKER") == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


# ---------------------------------------------------------------------------
# Servers and databases (plain helpers, shared with benchmarks/data)
# ---------------------------------------------------------------------------


@dataclass
class RunningServer:
    """A started server lane: its admin URL, and the container to stop when this run started one."""

    lane: str
    url: str
    container: Any = None

    def stop(self) -> None:
        """Stop and remove the container this run started; a ``PYFLY_IT_*`` server is left alone."""
        if self.container is not None:
            with contextlib.suppress(Exception):
                self.container.stop()
            self.container = None


def _with_driver(url: str, drivername: str) -> str:
    return make_url(url).set(drivername=drivername).render_as_string(hide_password=False)


def start_server(lane: str) -> RunningServer:
    """Provide the server for *lane*: the ``PYFLY_IT_*`` override URL, or a new testcontainer.

    The returned URL points at the server's default database with an account that may create and drop
    databases (the container superuser or root). Its driver is the lane's async driver.
    """
    spec = SERVER_SPECS[lane]
    override = os.environ.get(spec.env_var)
    if override:
        return RunningServer(lane, _with_driver(override, spec.drivername))
    if lane == PG:
        container = postgres_container(spec.image)
    elif lane == MYSQL:
        container = mysql_container(spec.image, username="root", password="pyfly", dbname="pyfly")
    else:
        container = mariadb_container(spec.image, username="root", password="pyfly", dbname="pyfly")
    try:
        container.start()
        url = _with_driver(pyfly_config_for(container)["pyfly.data.relational.url"], spec.drivername)
    except BaseException:
        with contextlib.suppress(Exception):  # a half-started container must not outlive the failure
            container.stop()
        raise
    return RunningServer(lane, url, container)


def new_database_name() -> str:
    """A unique, quoting-free database name."""
    return f"{_DATABASE_PREFIX}{uuid.uuid4().hex[:12]}"


async def create_database(server_url: str, name: str) -> str:
    """Create database *name* on the server behind *server_url* and return an async URL for it."""
    url = make_url(server_url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if url.get_backend_name() == "postgresql":
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
            else:
                await conn.execute(text(f"CREATE DATABASE `{name}`"))
    finally:
        await engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


async def drop_database(server_url: str, name: str) -> None:
    """Drop database *name*, first ending any session still connected to it.

    A leaked session (the pinned repository session of F3/F4, for one) would otherwise hold locks
    that make ``DROP DATABASE`` wait: PostgreSQL gets ``WITH (FORCE)``, and on MySQL/MariaDB every
    connection using the database is killed first.
    """
    url = make_url(server_url)
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if url.get_backend_name() == "postgresql":
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
                return
            await conn.execute(text("SET SESSION lock_wait_timeout = 30"))
            rows = await conn.execute(
                text("SELECT id FROM information_schema.processlist WHERE db = :name AND id <> CONNECTION_ID()"),
                {"name": name},
            )
            for (connection_id,) in rows.all():
                with contextlib.suppress(Exception):  # it may have ended on its own meanwhile
                    await conn.execute(text(f"KILL {int(connection_id)}"))
            await conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
    finally:
        await engine.dispose()


def enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Turn ``PRAGMA foreign_keys`` on for every connection *engine* opens (SQLite leaves it off)."""

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys_on(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# ---------------------------------------------------------------------------
# Backends handed to tests
# ---------------------------------------------------------------------------


@dataclass
class RelationalBackend:
    """One relational lane, bound to a database that belongs to the current test.

    ``url`` is an async SQLAlchemy URL for that database. Build engines with :meth:`create_engine`
    (they get the lane's settings and are disposed after the test), and an ``ApplicationContext``
    configuration with :meth:`config`.
    """

    lane: str
    url: str
    _engines: list[AsyncEngine] = field(default_factory=list, repr=False)

    @property
    def dialect(self) -> str:
        """The SQLAlchemy backend name of the URL: ``sqlite``, ``postgresql``, ``mysql`` or ``mariadb``."""
        return make_url(self.url).get_backend_name()

    @property
    def driver(self) -> str:
        """The DBAPI driver of the URL, e.g. ``aiosqlite``, ``asyncpg``, ``asyncmy``."""
        return make_url(self.url).get_driver_name()

    @property
    def is_embedded(self) -> bool:
        """Whether the lane needs no server (sqlite-file)."""
        return self.lane == SQLITE_FILE

    @property
    def pre_ping(self) -> bool:
        """Whether the lane runs with pool pre-ping on: MySQL and MariaDB do, because C089 broke it there."""
        return self.lane in (MYSQL, MARIADB)

    def engine_options(self) -> dict[str, Any]:
        """The ``create_async_engine`` options every engine on this lane gets."""
        return {"pool_pre_ping": True} if self.pre_ping else {}

    def create_engine(self, **options: Any) -> AsyncEngine:
        """An engine on this lane's database, disposed when the test ends.

        SQLite engines get ``PRAGMA foreign_keys=ON`` on every connection. *options* override the lane
        defaults (for example ``poolclass=NullPool`` or ``pool_pre_ping=False``).
        """
        engine = create_async_engine(self.url, **{**self.engine_options(), **options})
        if self.is_embedded:
            enable_sqlite_foreign_keys(engine)
        self._engines.append(engine)
        return engine

    def with_driver(self, driver: str) -> RelationalBackend:
        """The same database reached through another DBAPI driver (``asyncmy`` or ``aiomysql`` on MySQL)."""
        url = make_url(self.url)
        return RelationalBackend(self.lane, _with_driver(self.url, f"{url.get_backend_name()}+{driver}"), self._engines)

    def config(self, overrides: Mapping[str, Any] | None = None) -> Config:
        """A PyFly :class:`Config` with this database as the primary relational datasource.

        ``ddl-auto`` defaults to ``none`` here: create the tables a test needs with
        :meth:`create_tables`, because ``Base.metadata`` holds every model imported in the run.
        MySQL/MariaDB lanes set ``pool.pre-ping``. *overrides* are flat dotted keys, applied last.

        The engine the application builds from this configuration has the framework's own settings,
        not the lane's: on sqlite-file, foreign keys are enforced there only if the framework turns
        them on. That keeps a test of the framework honest; :meth:`create_engine` is the lane's engine.
        """
        flat: dict[str, Any] = {
            "pyfly.data.relational.enabled": "true",
            "pyfly.data.relational.url": self.url,
            "pyfly.data.relational.ddl-auto": "none",
        }
        if self.pre_ping:
            flat["pyfly.data.relational.pool.pre-ping"] = "true"
        flat.update(overrides or {})
        return pyfly_config(base=flat)

    async def create_tables(self, *models: Any, engine: AsyncEngine | None = None) -> None:
        """Create the tables of *models* (mapped classes or ``Table`` objects), in dependency order."""
        tables = [model if isinstance(model, Table) else model.__table__ for model in models]
        if not tables:
            return
        target = engine or create_async_engine(self.url, poolclass=NullPool)
        try:
            async with target.begin() as conn:
                await conn.run_sync(tables[0].metadata.create_all, tables=tables)
        finally:
            if engine is None:
                await target.dispose()

    async def dispose(self) -> None:
        """Dispose every engine :meth:`create_engine` built."""
        while self._engines:
            await self._engines.pop().dispose()


@dataclass(frozen=True)
class MongoBackend:
    """The MongoDB replica-set lane, bound to a database that belongs to the current test."""

    url: str
    database: str

    def config(self, overrides: Mapping[str, Any] | None = None) -> Config:
        """A PyFly :class:`Config` pointing the document module at this database."""
        flat: dict[str, Any] = {
            "pyfly.data.document.enabled": "true",
            "pyfly.data.document.uri": self.url,
            "pyfly.data.document.database": self.database,
        }
        flat.update(overrides or {})
        return pyfly_config(base=flat)


# ---------------------------------------------------------------------------
# pytest plugin: parametrization, markers, fixtures
# ---------------------------------------------------------------------------


def lane_marks(lane: str) -> list[pytest.MarkDecorator]:
    """The marks a matrix run on *lane* carries."""
    marks = [getattr(pytest.mark, LANE_MARKERS[lane])]
    if lane != SQLITE_FILE:
        marks += [pytest.mark.integration, pytest.mark.docker]
    return marks


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize ``relational_backend`` over the lanes, honoring ``@pytest.mark.backends(...)``."""
    if "relational_backend" not in metafunc.fixturenames:
        return
    marker = metafunc.definition.get_closest_marker("backends")
    lanes = tuple(marker.args) if marker is not None else RELATIONAL_LANES
    unknown = [lane for lane in lanes if lane not in RELATIONAL_LANES]
    if unknown or not lanes:
        raise pytest.UsageError(
            f"{metafunc.definition.nodeid}: @pytest.mark.backends takes lanes from {RELATIONAL_LANES}, got {lanes}"
        )
    metafunc.parametrize(
        "relational_backend",
        [pytest.param(lane, id=lane, marks=lane_marks(lane)) for lane in lanes],
        indirect=True,
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Give every test that uses a server fixture the ``integration`` marker and its lane marker."""
    for item in items:
        fixturenames = getattr(item, "fixturenames", ())
        for fixture_name, lane in FIXTURE_LANES.items():
            if fixture_name in fixturenames:
                item.add_marker(pytest.mark.integration)
                item.add_marker(pytest.mark.docker)
                item.add_marker(getattr(pytest.mark, LANE_MARKERS.get(lane, lane)))


def _serve(lane: str) -> Iterator[str]:
    try:
        server = start_server(lane)
    except Exception as exc:  # noqa: BLE001 — daemon present but cannot run/pull -> skip (or fail in CI)
        unavailable(f"{SERVER_SPECS[lane].title} testcontainer unavailable: {exc}")
    try:
        yield server.url
    finally:
        server.stop()


@pytest.fixture(scope="session")
def pg_server_url() -> Iterator[str]:
    """Admin URL of the PostgreSQL 17 server shared by the whole run (superuser, default database)."""
    yield from _serve(PG)


@pytest.fixture(scope="session")
def mysql_server_url() -> Iterator[str]:
    """Admin URL of the MySQL 8 server shared by the whole run (root, database ``pyfly``)."""
    yield from _serve(MYSQL)


@pytest.fixture(scope="session")
def mariadb_server_url() -> Iterator[str]:
    """Admin URL of the MariaDB 11 server shared by the whole run (root, database ``pyfly``)."""
    yield from _serve(MARIADB)


@pytest.fixture
async def relational_backend(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[RelationalBackend]:
    """The current lane with a database of its own (parametrized by :func:`pytest_generate_tests`)."""
    lane: str = request.param
    if lane == SQLITE_FILE:
        backend = RelationalBackend(lane, f"sqlite+aiosqlite:///{tmp_path / 'pyfly.db'}")
        try:
            yield backend
        finally:
            await backend.dispose()
        return
    server_url: str = request.getfixturevalue(f"{lane}_server_url")
    name = new_database_name()
    backend = RelationalBackend(lane, await create_database(server_url, name))
    try:
        yield backend
    finally:
        await backend.dispose()
        await drop_database(server_url, name)


@pytest.fixture(scope="session")
def mongo_rs_url() -> Iterator[str]:
    """URL of the MongoDB 7 single-node replica set (``rs0``) shared by the whole run.

    It carries ``directConnection=true``: the client talks to that one member and skips replica-set
    discovery, so the member's advertised host does not have to resolve from the test process.
    """
    override = os.environ.get(MONGO_ENV_VAR)
    if override:
        yield override
        return
    try:
        container = mongodb_replica_set_container(MONGO_IMAGE)
        container.start()
    except Exception as exc:  # noqa: BLE001 — daemon present but cannot run/pull -> skip (or fail in CI)
        unavailable(f"MongoDB replica-set testcontainer unavailable: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        with contextlib.suppress(Exception):
            container.stop()


@pytest.fixture
def mongo_backend(mongo_rs_url: str) -> Iterator[MongoBackend]:
    """The replica-set lane with a database of its own, dropped after the test."""
    from pymongo import MongoClient

    backend = MongoBackend(url=mongo_rs_url, database=new_database_name())
    try:
        yield backend
    finally:
        client: MongoClient[Any] = MongoClient(mongo_rs_url, serverSelectionTimeoutMS=10_000)
        try:
            client.drop_database(backend.database)
        finally:
            client.close()
