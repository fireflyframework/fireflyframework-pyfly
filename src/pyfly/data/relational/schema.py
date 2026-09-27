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
"""The schema strategy of the primary datasource (``pyfly.data.relational.ddl-auto``), and the lock that lets
one instance at a time change a database's schema.

:class:`SchemaInitializer` applies the strategy to the entity models (``Base.metadata``) when the context
starts:

=================  =============================================================================================
Strategy           What happens
=================  =============================================================================================
``none``           Nothing: migrations own the schema.
``validate``       Every table and column of the models must exist, or the start fails with
                   :class:`SchemaValidationError` naming what is missing (a migration that was never written).
                   A different column type or nullability is logged (``schema_validation_differences``).
``create``         The missing tables are created (``create_all``); existing tables are never altered.
``create-drop``    As ``create``, and the tables are dropped when the context stops.
=================  =============================================================================================

The strategy defaults to ``create`` for an embedded database (SQLite) and to ``none`` for a database server or
when startup migrations are enabled; any other value fails the start
(:func:`~pyfly.config.properties.data.ddl_auto_strategy`). The framework's own tables follow the same
strategy: their stores create them when it is ``create`` or ``create-drop`` and only check them otherwise
(:func:`~pyfly.data.relational.framework_schema.creates_tables`), and they are never dropped.

Order. The startup migrations (:class:`~pyfly.data.relational.migrations.MigrationRunner`) are lifecycle beans
of :data:`~pyfly.data.relational.migrations.MIGRATION_PHASE`; the schema strategy runs in
:data:`SCHEMA_PHASE`, right after them; the stores that check their tables, and every other lifecycle bean,
start after both. The ``create-drop`` teardown runs after every one of those beans has stopped.

Instances that start together take :func:`schema_lock` around the migrations and around the strategy, so
one instance changes the schema while the others wait (``pyfly.data.relational.schema.lock-timeout``), then
find it done:

- PostgreSQL: a session advisory lock (``pg_try_advisory_lock``), local to the database;
- MySQL and MariaDB: a named lock (``GET_LOCK``), named after the database;
- SQLite: every schema transaction starts with ``BEGIN IMMEDIATE``, which takes the database's write lock;
- any other backend: a lease of the framework's lock table (:class:`LeaseSchemaLock`). The table is created
  when the strategy allows it; without it the changes are not serialized, with a WARNING.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
from collections.abc import AsyncIterator, Iterable
from typing import Any, Literal

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from pyfly.config.properties.data import ddl_auto_strategy
from pyfly.data.relational.dialect_customizers import SQLITE_BEGIN_OPTION, uses_sqlite_begin_recipe
from pyfly.data.relational.migrations import MIGRATION_PHASE

__all__ = [
    "SCHEMA_LOCK",
    "SCHEMA_PHASE",
    "LeaseSchemaLock",
    "SchemaInitializer",
    "SchemaLockTimeoutError",
    "SchemaValidationError",
    "schema_lock",
    "schema_transaction",
]

_logger = logging.getLogger(__name__)

SCHEMA_PHASE = MIGRATION_PHASE + 1
"""The lifecycle phase of :class:`SchemaInitializer`: right after the startup migrations and before every
other lifecycle bean (the framework stores check their tables in the default phase), and stopped after them."""

SCHEMA_LOCK = "pyfly_schema"
"""The name of the lock :func:`schema_lock` takes (a lease name on the lease table)."""

LockStrategy = Literal["auto", "lease"]
"""How :func:`schema_lock` serializes: the backend's own lock (``auto``), or a lease of the lock table."""

_DROP_GRACE = 5.0
"""Seconds the ``create-drop`` teardown waits beyond the database's own lock timeout before it gives up."""


class SchemaValidationError(RuntimeError):
    """``ddl-auto=validate`` found tables or columns of the entity models missing from the database."""


class SchemaLockTimeoutError(TimeoutError):
    """Another instance held the schema lock for longer than ``pyfly.data.relational.schema.lock-timeout``."""


def backend_name(dialect: Dialect) -> str:
    """``mariadb`` for a MariaDB server (a ``mysql://`` URL included), else the dialect's name."""
    return "mariadb" if getattr(dialect, "is_mariadb", False) else str(dialect.name)


# ---------------------------------------------------------------------------------------------------------
# The schema lock
# ---------------------------------------------------------------------------------------------------------


def _advisory_key(name: str) -> int:
    """A stable signed 64-bit key for *name* (PostgreSQL advisory locks take a ``bigint``)."""
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


@contextlib.asynccontextmanager
async def schema_lock(
    connection: AsyncConnection,
    *,
    timeout: float,
    lock: LockStrategy = "auto",
    create_lease_table: bool = False,
) -> AsyncIterator[None]:
    """Hold the schema lock of *connection*'s database while the block runs (see the module documentation).

    The lock is taken on *connection* itself where the backend has one (PostgreSQL, MySQL, MariaDB), and the
    connection is not left inside a transaction: the block runs its own. Waiting longer than *timeout*
    seconds raises :class:`SchemaLockTimeoutError`. *lock* ``lease`` uses the lock table on every backend;
    *create_lease_table* lets the lease create that table when it is missing.
    """
    backend = backend_name(connection.dialect)
    if lock == "lease" or backend not in ("postgresql", "mysql", "mariadb", "sqlite"):
        lease = LeaseSchemaLock(connection.engine, create_table=create_lease_table)
        async with lease.hold(timeout):
            yield
        return
    if backend == "sqlite":
        # One writer per database: every schema transaction starts with BEGIN IMMEDIATE (schema_transaction).
        yield
        return
    if backend == "postgresql":
        async with _postgresql_lock(connection, timeout):
            yield
        return
    async with _mysql_lock(connection, timeout):
        yield


async def _end_transaction(connection: AsyncConnection) -> None:
    if connection.in_transaction():
        await connection.commit()


@contextlib.asynccontextmanager
async def _postgresql_lock(connection: AsyncConnection, timeout: float) -> AsyncIterator[None]:
    key = _advisory_key(SCHEMA_LOCK)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    delay = 0.05
    while True:
        acquired = (await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})).scalar()
        await _end_transaction(connection)  # a session lock: it outlives the transaction the query began
        if acquired:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise SchemaLockTimeoutError(
                f"Another instance held the schema lock of {_where(connection)} for more than {timeout:g}s "
                "(pyfly.data.relational.schema.lock-timeout)"
            )
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 1.0)
    try:
        yield
    finally:
        await _release(connection, "SELECT pg_advisory_unlock(:key)", {"key": key})


# MySQL and MariaDB name their locks server-wide: the name carries the database, so two applications on one
# server do not wait for each other. SHA1 keeps it within the 64 characters a lock name may have.
_MYSQL_LOCK_NAME = "CONCAT(:prefix, LEFT(SHA1(COALESCE(DATABASE(), '')), 40))"


@contextlib.asynccontextmanager
async def _mysql_lock(connection: AsyncConnection, timeout: float) -> AsyncIterator[None]:
    prefix = f"{SCHEMA_LOCK}:"
    seconds = max(0, math.ceil(timeout))
    acquired = (
        await connection.execute(
            text(f"SELECT GET_LOCK({_MYSQL_LOCK_NAME}, :timeout)"), {"prefix": prefix, "timeout": seconds}
        )
    ).scalar()
    await _end_transaction(connection)  # a named lock belongs to the session, not to the transaction
    if acquired != 1:
        raise SchemaLockTimeoutError(
            f"Another instance held the schema lock of {_where(connection)} for more than {timeout:g}s "
            "(pyfly.data.relational.schema.lock-timeout)"
        )
    try:
        yield
    finally:
        await _release(connection, f"SELECT RELEASE_LOCK({_MYSQL_LOCK_NAME})", {"prefix": prefix})


async def _release(connection: AsyncConnection, statement: str, parameters: dict[str, Any]) -> None:
    """Release a session lock. When that fails the connection is invalidated, never returned to the pool: the
    pool's reset (a rollback) keeps a session lock, and the next checkout would hold it without knowing; a closed
    connection releases it on the server."""
    try:
        if connection.in_transaction():
            await connection.rollback()
        await connection.execute(text(statement), parameters)
        await _end_transaction(connection)
    except DBAPIError:
        _logger.warning("schema_lock_release_failed", exc_info=True)
        with contextlib.suppress(Exception):
            await connection.invalidate()


def _where(connection: AsyncConnection) -> str:
    return connection.engine.url.render_as_string(hide_password=True)


class LeaseSchemaLock:
    """The portable schema lock: the lease :data:`SCHEMA_LOCK` of the framework's lock table (``pyfly_locks``,
    :class:`~pyfly.scheduling.adapters.lease_lock.LeaseLock`), renewed while it is held.

    A lease that is not renewed (its holder crashed) ends after :attr:`TTL` seconds, and a waiting instance
    takes it over. With *create_table* the lock table is created when it is missing; without it and without
    the table, the schema changes run unserialized, with a WARNING (``schema_changes_not_serialized``).
    """

    #: Seconds a lease lasts unless renewed; it is renewed every third of that while the block runs.
    TTL = 60.0

    def __init__(self, engine: AsyncEngine, *, create_table: bool = False) -> None:
        self._engine = engine
        self._create_table = create_table

    @contextlib.asynccontextmanager
    async def hold(self, timeout: float) -> AsyncIterator[None]:
        """Hold the lease while the block runs; waiting longer than *timeout* raises
        :class:`SchemaLockTimeoutError`."""
        from pyfly.data.relational.framework_schema import FrameworkSchemaError
        from pyfly.scheduling.adapters.lease_lock import LeaseLock

        lock = LeaseLock(self._engine, create_table=self._create_table)
        try:
            await lock.start()
        except FrameworkSchemaError as error:
            _logger.warning(
                "schema_changes_not_serialized",
                extra={
                    "datasource": self._engine.url.render_as_string(hide_password=True),
                    "reason": str(error),
                    "hint": "instances that start together may change the schema at the same time; create the "
                    "framework lock table (pyfly_locks) with a migration",
                },
            )
            unserialized = True
        else:
            unserialized = False
        if unserialized:
            yield
            return
        try:
            lease = await lock.acquire(SCHEMA_LOCK, self.TTL, wait=timeout)
            if lease is None:
                raise SchemaLockTimeoutError(
                    f"Another instance held the schema lease of "
                    f"{self._engine.url.render_as_string(hide_password=True)} for more than {timeout:g}s "
                    "(pyfly.data.relational.schema.lock-timeout)"
                )
            renewal = asyncio.create_task(self._renew(lock))
            try:
                yield
            finally:
                renewal.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal
                await lock.release(SCHEMA_LOCK)
        finally:
            await lock.stop()

    async def _renew(self, lock: Any) -> None:
        while True:
            await asyncio.sleep(self.TTL / 3)
            try:
                if not await lock.extend(SCHEMA_LOCK, self.TTL):
                    _logger.warning("schema_lease_lost", extra={"lease": SCHEMA_LOCK})
                    return
            except Exception:  # noqa: BLE001 — a failed renewal must not end the schema work it guards
                _logger.warning("schema_lease_renewal_failed", exc_info=True)


@contextlib.asynccontextmanager
async def schema_transaction(connection: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """A transaction for schema changes on *connection*: on SQLite it starts with ``BEGIN IMMEDIATE`` (the
    write lock is taken first, so a second instance waits for the first one's changes instead of racing
    them). It commits when the block succeeds and rolls back otherwise."""
    sqlite = connection.dialect.name == "sqlite"
    recipe = sqlite and uses_sqlite_begin_recipe(connection.engine)
    if recipe:
        await connection.execution_options(**{SQLITE_BEGIN_OPTION: "IMMEDIATE"})
    async with connection.begin():
        if sqlite and not recipe:
            # The plain driver defers its own BEGIN until the first write.
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield connection


# ---------------------------------------------------------------------------------------------------------
# The schema strategy
# ---------------------------------------------------------------------------------------------------------


class SchemaInitializer:
    """Applies the schema strategy *ddl_auto* to *metadata* (``Base.metadata`` by default) on *engine*'s
    database (see the module documentation).

    *ddl_auto* is resolved with :func:`~pyfly.config.properties.data.ddl_auto_strategy` against the engine's
    URL (an invalid value raises ``ValueError`` here, when the context builds the bean); *migrations* says
    whether startup migrations are enabled. *lock_timeout* bounds the wait for another instance's schema
    changes, *drop_timeout* the ``create-drop`` teardown. *lock* ``lease`` serializes on the lock table even
    where the backend has a lock of its own.
    """

    #: Started right after the startup migrations, before every other lifecycle bean, and stopped last.
    phase = SCHEMA_PHASE

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        ddl_auto: str | None = None,
        metadata: MetaData | None = None,
        migrations: bool = False,
        lock_timeout: float = 300.0,
        drop_timeout: float = 10.0,
        lock: LockStrategy = "auto",
    ) -> None:
        self._engine = engine
        self._ddl_auto = ddl_auto_strategy(
            ddl_auto, url=engine.url.render_as_string(hide_password=True), migrations=migrations
        )
        self._metadata = metadata
        self._lock_timeout = lock_timeout
        self._drop_timeout = drop_timeout
        self._lock: LockStrategy = lock

    @property
    def ddl_auto(self) -> str:
        """The effective strategy: ``none``, ``validate``, ``create`` or ``create-drop``."""
        return self._ddl_auto

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the database the strategy applies to."""
        return self._engine

    def _models(self) -> MetaData:
        if self._metadata is not None:
            return self._metadata
        from pyfly.data.relational.sqlalchemy.entity import Base

        return Base.metadata

    # -- start ------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Validate the schema, or create the missing tables, under the schema lock."""
        if self._ddl_auto == "none":
            return
        async with (
            self._engine.connect() as connection,
            schema_lock(
                connection,
                timeout=self._lock_timeout,
                lock=self._lock,
                create_lease_table=self._ddl_auto != "validate",
            ),
        ):
            if self._ddl_auto == "validate":
                await self._validate(connection)
            else:
                await self._create(connection)

    async def _create(self, connection: AsyncConnection) -> None:
        metadata = self._models()
        _logger.info("Initializing database schema (ddl-auto=%s)", self._ddl_auto)
        async with schema_transaction(connection):
            await connection.run_sync(metadata.create_all)
        _logger.info("Database schema initialized (%d tables)", len(metadata.tables))

    async def _validate(self, connection: AsyncConnection) -> None:
        metadata = self._models()
        async with connection.begin():
            missing, differences = await connection.run_sync(_schema_differences, metadata)
        where = self._engine.url.render_as_string(hide_password=True)
        if differences:
            _logger.warning(
                "schema_validation_differences",
                extra={
                    "datasource": where,
                    "differences": differences,
                    "hint": "the database's column types or nullability differ from the entity models; a "
                    "migration can align them",
                },
            )
        if missing:
            raise SchemaValidationError(
                f"ddl-auto=validate: the database {where} is missing " + "; ".join(missing) + ". Write the migration "
                "that creates them (pyfly db migrate) and apply it (pyfly db upgrade, or "
                "pyfly.data.relational.migrations.enabled=true)."
            )
        _logger.info("Database schema validated (%d tables)", len(metadata.tables))

    # -- stop -------------------------------------------------------------------------------------------

    async def stop(self) -> None:
        """Drop the entity tables for ``create-drop``; a drop that fails or times out is logged, never raised."""
        if self._ddl_auto != "create-drop":
            return
        metadata = self._models()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._drop_timeout
        await self._wait_for_units(deadline)
        seconds = max(1.0, deadline - loop.time())
        _logger.info("Dropping database schema (ddl-auto=create-drop)")
        try:
            async with asyncio.timeout(seconds + _DROP_GRACE), self._engine.connect() as connection:
                dropped = await _drop_tables(connection, metadata, seconds)
        except (DBAPIError, TimeoutError, OSError) as error:
            _logger.warning(
                "schema_drop_failed",
                extra={
                    "datasource": self._engine.url.render_as_string(hide_password=True),
                    "error": f"{type(error).__name__}: {error}",
                    "hint": "another connection held a lock on the tables for longer than "
                    "pyfly.data.relational.schema.drop-timeout; the schema is left as it is",
                },
            )
            return
        _logger.info("Database schema dropped (%d tables)", dropped)

    async def _wait_for_units(self, deadline: float) -> None:
        """Wait (until *deadline*) for the units of work still running on this database to return their
        connections: a connection inside a transaction holds locks the drop would wait for."""
        pools = [engine.sync_engine.pool for engine in self._same_database_engines()]
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            if not any(getattr(pool, "checkedout", lambda: 0)() for pool in pools):
                return
            await asyncio.sleep(0.05)
        in_use = sum(getattr(pool, "checkedout", lambda: 0)() for pool in pools)
        if in_use:
            _logger.warning(
                "schema_drop_with_connections_in_use",
                extra={"connections": in_use, "hint": "the drop waits for their locks up to the drop timeout"},
            )

    def _same_database_engines(self) -> list[AsyncEngine]:
        """This engine, and the other engines of its registry on the same database (a named datasource or a
        module datasource on the primary's URL)."""
        from pyfly.data.relational.datasource_registry import datasource_of

        engines = [self._engine]
        datasource = datasource_of(self._engine)
        registry = datasource.registry if datasource is not None else None
        if registry is None or registry.closed:
            return engines
        mine = _database_identity(self._engine)
        for other in registry.all_datasources():
            if other.engine is not self._engine and _database_identity(other.engine) == mine:
                engines.append(other.engine)
        return engines


def _database_identity(engine: AsyncEngine) -> tuple[Any, ...]:
    url = engine.url
    return (url.get_backend_name(), url.host, url.port, url.database)


def _schema_differences(connection: Connection, metadata: MetaData) -> tuple[list[str], list[str]]:
    """What the database lacks (tables, columns), and how its existing columns differ, from *metadata*."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    schemas = {table.schema for table in metadata.tables.values() if table.schema}
    options: dict[str, Any] = {"compare_type": True}
    if schemas:
        options["include_schemas"] = True
        options["include_name"] = lambda name, kind, _parent: kind != "schema" or name is None or name in schemas
    context = MigrationContext.configure(connection, opts=options)
    missing: list[str] = []
    differences: list[str] = []
    for diff in _flatten(compare_metadata(context, metadata)):
        kind = diff[0]
        if kind == "add_table":
            missing.append(f"table {_qualified(diff[1].schema, diff[1].name)}")
        elif kind == "add_column":
            missing.append(f"column {_qualified(diff[1], diff[2])}.{diff[3].name}")
        elif kind == "modify_type":
            differences.append(f"{_qualified(diff[1], diff[2])}.{diff[3]}: {diff[5]} in the database, {diff[6]} mapped")
        elif kind == "modify_nullable":
            nullable = "nullable" if diff[5] else "not nullable"
            differences.append(f"{_qualified(diff[1], diff[2])}.{diff[3]}: {nullable} in the database")
    return missing, differences


def _flatten(diffs: Iterable[Any]) -> Iterable[tuple[Any, ...]]:
    """Alembic groups the modifications of one column in a list."""
    for diff in diffs:
        if isinstance(diff, list):
            yield from diff
        else:
            yield diff


def _qualified(schema: str | None, name: str) -> str:
    return f"{schema}.{name}" if schema else name


async def _drop_tables(connection: AsyncConnection, metadata: MetaData, seconds: float) -> int:
    """Drop the tables of *metadata* that exist, waiting at most about *seconds* for the locks the drop needs;
    return how many were dropped.

    The wait is bounded by the database itself, so a drop that gives up never runs later: PostgreSQL gets
    ``lock_timeout`` and locks every table in one statement first; MySQL and MariaDB get
    ``lock_wait_timeout`` and drop every table in one statement; SQLite gets ``busy_timeout``.
    """
    backend = backend_name(connection.dialect)
    if backend == "postgresql":
        async with connection.begin():
            await connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{max(1, int(seconds * 1000))}ms'")
            tables = await connection.run_sync(_existing_tables, metadata)
            if tables:
                preparer = connection.dialect.identifier_preparer
                names = ", ".join(preparer.format_table(table) for table in tables)
                await connection.exec_driver_sql(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")
                await connection.run_sync(lambda sync: metadata.drop_all(sync, tables=tables))
        return len(tables)
    if backend in ("mysql", "mariadb"):
        await connection.exec_driver_sql(f"SET SESSION lock_wait_timeout = {max(1, math.ceil(seconds))}")
        try:
            tables = await connection.run_sync(_existing_tables, metadata)
            await _end_transaction(connection)
            if tables:
                preparer = connection.dialect.identifier_preparer
                # Children first; one statement takes every metadata lock at once, within lock_wait_timeout.
                names = ", ".join(preparer.format_table(table) for table in reversed(tables))
                await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {names}")
                await _end_transaction(connection)
        finally:
            with contextlib.suppress(DBAPIError):
                await connection.exec_driver_sql("SET SESSION lock_wait_timeout = DEFAULT")
                await _end_transaction(connection)
        return len(tables)
    if backend == "sqlite":
        previous = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()
        await connection.exec_driver_sql(f"PRAGMA busy_timeout = {max(1, int(seconds * 1000))}")
        await _end_transaction(connection)
        try:
            async with schema_transaction(connection):
                tables = await connection.run_sync(_existing_tables, metadata)
                await connection.run_sync(lambda sync: metadata.drop_all(sync, tables=tables))
        finally:
            with contextlib.suppress(DBAPIError):
                await connection.exec_driver_sql(f"PRAGMA busy_timeout = {int(previous or 0)}")
                await _end_transaction(connection)
        return len(tables)
    async with connection.begin():
        tables = await connection.run_sync(_existing_tables, metadata)
        await connection.run_sync(lambda sync: metadata.drop_all(sync, tables=tables))
    return len(tables)


def _existing_tables(connection: Connection, metadata: MetaData) -> list[Table]:
    """The tables of *metadata* that exist, parents first."""
    inspector = inspect(connection)
    return [table for table in metadata.sorted_tables if inspector.has_table(table.name, schema=table.schema)]
