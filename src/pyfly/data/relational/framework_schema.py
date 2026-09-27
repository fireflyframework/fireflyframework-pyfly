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
"""The framework's own SQL tables, declared once as SQLAlchemy Core tables on one ``MetaData``.

Every table PyFly keeps in an application's database (``pyfly_*``) is a :class:`~sqlalchemy.Table` on
:data:`framework_metadata`, with column types that work on every backend:

- bounded :func:`key_string` keys (MySQL and MariaDB cannot index an unbounded ``TEXT`` column);
- :class:`UtcTimestamp` instants: UTC with microseconds everywhere, aware in Python;
- :func:`long_text` and :func:`long_binary` payloads (``LONGTEXT``/``LONGBLOB`` on MySQL and MariaDB,
  whose ``TEXT``/``BLOB`` stop at 64 KiB);
- a naming convention, so constraints and indexes get the same name on every backend.

Alembic sees them through the metadata. An ``env.py`` whose ``target_metadata`` lists the application's
metadata and this one (``target_metadata = [Base.metadata, framework_metadata]``) autogenerates their
``CREATE TABLE`` and never a ``DROP TABLE`` for them.

The tables, and who uses them:

=============================  ====================================================================
Table                          Used by
=============================  ====================================================================
``pyfly_orchestration_state``  ``SqlAlchemyPersistenceProvider`` (saga, TCC and workflow state)
``pyfly_cache_entries``        ``PostgresCacheAdapter`` (``pyfly.cache.provider=postgres``)
``pyfly_locks``                ``LeaseLock`` (``@scheduled(lock=...)`` and other leases)
``pyfly_users``                ``SqlUserDetailsService``
=============================  ====================================================================

A store that is configured with another table name declares that table here too, through the table's
factory function (:func:`orchestration_state_table` and the others), so a migration environment that
builds the store's table the same way sees it.

Stores call :func:`ensure_tables` when they start: it creates their tables when the schema strategy
allows it (:func:`creates_tables`: ``pyfly.data.relational.ddl-auto`` is ``create``, ``create-drop`` or
``update``; with ``none``, ``validate`` or any other value the tables are left to migrations), then checks
that every table and column is there, and fails fast with :class:`FrameworkSchemaError` naming what is
missing. Declaring a new framework table: add a factory function next to the others (a ``Table`` on
:data:`framework_metadata` built by :func:`_declare`), its module-level default table, and a row above.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Unicode,
    inspect,
    text,
)
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.types import TypeDecorator, TypeEngine

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry

__all__ = [
    "CACHE_ENTRIES",
    "CREATE_ATTEMPTS",
    "FRAMEWORK_TABLE_PREFIX",
    "LOCKS",
    "NAMING_CONVENTION",
    "ORCHESTRATION_STATE",
    "USERS",
    "FrameworkSchemaError",
    "UtcTimestamp",
    "cache_entries",
    "cache_entries_table",
    "context_datasource_registry",
    "creates_tables",
    "ensure_tables",
    "framework_engine",
    "framework_metadata",
    "locks",
    "locks_table",
    "key_string",
    "long_binary",
    "long_text",
    "module_datasource",
    "orchestration_state",
    "orchestration_state_table",
    "users",
    "users_table",
]

_logger = logging.getLogger(__name__)

FRAMEWORK_TABLE_PREFIX = "pyfly_"
"""The prefix of every table the framework declares under its default name."""

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}
"""Index and constraint names of the framework tables: identical on every backend, so one Alembic history
applies to SQLite, PostgreSQL and MySQL/MariaDB (SQLAlchemy shortens a name longer than the backend allows,
deterministically). Primary keys keep the backend's own name (``<table>_pkey`` on PostgreSQL, as the tables
created before this metadata have it; MySQL and MariaDB name every primary key ``PRIMARY``)."""

framework_metadata = MetaData(naming_convention=NAMING_CONVENTION)
"""The ``MetaData`` of every framework table (``pyfly_*``); list it in Alembic's ``target_metadata``."""

ORCHESTRATION_STATE = "pyfly_orchestration_state"
CACHE_ENTRIES = "pyfly_cache_entries"
LOCKS = "pyfly_locks"
USERS = "pyfly_users"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Backends whose timestamp column keeps no time zone: a UtcTimestamp is stored there as naive UTC.
_NAIVE_TIMESTAMP_DIALECTS = frozenset({"sqlite", "mysql", "mariadb"})

_KIND = "pyfly_framework_table"
"""``Table.info`` key: which framework table a ``Table`` is (a store's custom name included)."""


# ---------------------------------------------------------------------------------------------------------
# Column types
# ---------------------------------------------------------------------------------------------------------


class UtcTimestamp(TypeDecorator[datetime]):
    """A point in time, stored in UTC with microsecond precision on every backend.

    - PostgreSQL: ``TIMESTAMP WITH TIME ZONE``, bound as an aware UTC value (a naive value would be read as
      the client process's local time by asyncpg).
    - MySQL and MariaDB: ``DATETIME(6)`` (a plain ``DATETIME`` keeps whole seconds), holding naive UTC.
    - SQLite: SQLAlchemy's ISO text, holding naive UTC, so comparisons in SQL order correctly.
    - SQL Server and Oracle: their time-zone-aware timestamp types.

    A bound value may be aware in any zone (it is converted to UTC) or naive (it is taken as UTC). A loaded
    value is always aware, in UTC. The framework's tables use it for every instant; the application's
    entities get the same guarantee from the entity types of the ORM layer.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        if dialect.name == "oracle":
            return dialect.type_descriptor(TIMESTAMP(timezone=True))  # a DateTime is a DATE (whole seconds) there
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UtcTimestamp takes a datetime, got {type(value).__name__}")
        instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return instant.replace(tzinfo=None) if dialect.name in _NAIVE_TIMESTAMP_DIALECTS else instant

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def key_string(length: int = 255) -> TypeEngine[str]:
    """A key column: ``VARCHAR(length)``, ``NVARCHAR`` on SQL Server (whose ``VARCHAR`` is not Unicode)."""
    return Unicode(length)


def long_text() -> TypeEngine[str]:
    """Unbounded text: ``TEXT``, ``LONGTEXT`` on MySQL and MariaDB (whose ``TEXT`` holds 64 KiB) and
    ``NVARCHAR(max)`` on SQL Server."""
    return Text().with_variant(mysql.LONGTEXT(), "mysql", "mariadb").with_variant(mssql.NVARCHAR(), "mssql")


def long_binary() -> TypeEngine[bytes]:
    """Unbounded bytes: ``BYTEA``/``BLOB``, ``LONGBLOB`` on MySQL and MariaDB (whose ``BLOB`` holds 64 KiB)
    and ``VARBINARY(max)`` on SQL Server."""
    binary = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql", "mariadb")
    unbounded = mssql.VARBINARY()  # type: ignore[no-untyped-call]
    return binary.with_variant(unbounded, "mssql")


# ---------------------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------------------


def _declare(kind: str, name: str, build: Callable[[str], Table]) -> Table:
    """The framework table *kind* named *name* on :data:`framework_metadata`, declared on first use."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Invalid framework table name: {name!r} (letters, digits and underscores only)")
    existing = framework_metadata.tables.get(name)
    if existing is not None:
        if existing.info.get(_KIND) != kind:
            raise ValueError(
                f"Table {name!r} is already declared as the framework's {existing.info.get(_KIND)!r} table; "
                f"a {kind!r} table needs a name of its own"
            )
        return existing
    table = build(name)
    table.info[_KIND] = kind
    return table


def orchestration_state_table(name: str = ORCHESTRATION_STATE) -> Table:
    """The table of orchestration (saga, TCC, workflow) executions, one row per correlation id.

    ``payload`` is the execution's JSON (its full state); the other columns are what the recovery scan
    and the queries filter on.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("correlation_id", key_string(), primary_key=True),
            Column("execution_name", key_string(), nullable=False),
            Column("pattern", key_string(32), nullable=False),
            Column("status", key_string(32), nullable=False, index=True),
            Column("started_at", UtcTimestamp(), nullable=False),
            Column("updated_at", UtcTimestamp(), nullable=False, index=True),
            Column("completed_at", UtcTimestamp(), nullable=True),
            Column("payload", long_text(), nullable=False),
        )

    return _declare("orchestration_state", name, build)


def cache_entries_table(name: str = CACHE_ENTRIES) -> Table:
    """The table of the SQL cache: a serialized value per key, with an optional expiry.

    The key is ``TEXT`` on PostgreSQL and SQLite (as the adapter always created it there) and
    ``VARCHAR(512)`` elsewhere, where a key column needs a length.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("cache_key", key_string(512).with_variant(Text(), "postgresql", "sqlite"), primary_key=True),
            Column("value", long_binary(), nullable=False),
            Column("expires_at", UtcTimestamp(), nullable=True, index=True),
        )

    return _declare("cache_entries", name, build)


def locks_table(name: str = LOCKS) -> Table:
    """The lease table (ShedLock-style): a named lock is held by ``locked_by`` until ``lock_until``.

    ``fence`` grows by one at every acquisition: a fencing token a holder can compare to know it still
    holds the lease it took.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("name", key_string(), primary_key=True),
            Column("lock_until", UtcTimestamp(), nullable=False),
            Column("locked_at", UtcTimestamp(), nullable=False),
            Column("locked_by", key_string(), nullable=False),
            Column("fence", BigInteger(), nullable=False),
        )

    return _declare("locks", name, build)


def users_table(name: str = USERS) -> Table:
    """The table of ``SqlUserDetailsService``: credentials, and roles and permissions as JSON arrays."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("username", key_string(), primary_key=True),
            Column("password_hash", long_text(), nullable=False),
            Column("roles", long_text(), nullable=False),
            Column("permissions", long_text(), nullable=False),
            Column("enabled", Integer(), nullable=False, server_default=text("1")),
        )

    return _declare("users", name, build)


orchestration_state = orchestration_state_table()
"""``pyfly_orchestration_state`` (:func:`orchestration_state_table`)."""

cache_entries = cache_entries_table()
"""``pyfly_cache_entries`` (:func:`cache_entries_table`)."""

locks = locks_table()
"""``pyfly_locks`` (:func:`locks_table`)."""

users = users_table()
"""``pyfly_users`` (:func:`users_table`)."""


# ---------------------------------------------------------------------------------------------------------
# Creating and verifying the tables of a store
# ---------------------------------------------------------------------------------------------------------


class FrameworkSchemaError(RuntimeError):
    """A framework table a store needs is missing or does not have the expected columns."""


CREATE_ATTEMPTS = 5
"""How many times :func:`ensure_tables` tries to create the missing tables while other processes race it."""


_CREATING_STRATEGIES = frozenset({"create", "create-drop", "update"})


def creates_tables(ddl_auto: str | None) -> bool:
    """Whether a store creates its missing framework tables when it starts, under the schema strategy
    *ddl_auto* (``pyfly.data.relational.ddl-auto``): ``create``, ``create-drop`` and ``update`` do (the
    framework tables are never dropped); ``none``, ``validate`` and any other value leave the schema to
    migrations, and the store only checks it."""
    return str(ddl_auto or "").strip().lower() in _CREATING_STRATEGIES


def context_datasource_registry(config: Config, container: Container | None = None) -> DataSourceRegistry:
    """The ``DataSourceRegistry`` a framework store's auto-configuration resolves its datasource in: the
    context's bean (an application's singleton registry replaces the configuration's), or the registry of
    *config* when there is no container or no such bean.

    Several registry beans with no ``@primary`` among them raise ``NoUniqueBeanError``: building the
    configuration's registry instead would open a third set of pools beside the application's."""
    from pyfly.container.exceptions import NoSuchBeanError
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    if container is not None:
        try:
            return container.resolve(DataSourceRegistry)
        except NoSuchBeanError:
            pass
    return DataSourceRegistry.for_config(config)


def module_datasource(registry: DataSourceRegistry, config: Config, prefix: str, *, name: str) -> DataSource:
    """The datasource of the framework store configured under *prefix* (``pyfly.cache.postgres``...).

    - ``<prefix>.datasource`` names a datasource of *registry* (``primary`` or a named datasource);
    - ``<prefix>.url`` is an alias resolved through the registry: the registered datasource with that URL,
      or a new one registered as *name*, with the registry's pool settings;
    - with neither, the store is on the primary datasource.

    Setting both raises :class:`~pyfly.data.relational.datasource_registry.DataSourceConfigurationError`,
    and so does a name the registry does not know (:class:`NoSuchDataSourceError`).
    """
    from pyfly.data.relational.datasource_registry import DataSourceConfigurationError

    named = str(config.get(f"{prefix}.datasource", "") or "").strip()
    url = config.get(f"{prefix}.url")
    if named and url is not None and str(url).strip():
        raise DataSourceConfigurationError(
            f"{prefix}.datasource and {prefix}.url are both set; name the datasource or give its URL, not both"
        )
    if named:
        return registry.get(named)
    return registry.resolve(url, name=name, url_key=f"{prefix}.url")


def framework_engine(target: object) -> AsyncEngine:
    """The engine of *target*: an ``AsyncEngine``, a ``DataSource`` or a transaction manager (its
    ``engine``), a datasource name or ``None`` (the installed transaction managers' datasource)."""
    if isinstance(target, AsyncEngine):
        return target
    if target is None or isinstance(target, str):
        from pyfly.data.transaction.registry import resolve_manager

        target = resolve_manager(target)
    engine = getattr(target, "engine", None)
    if isinstance(engine, AsyncEngine):
        return engine
    raise TypeError(
        f"Expected an AsyncEngine, a DataSource, a SQLAlchemy transaction manager or a datasource name, "
        f"got {type(target).__name__}"
    )


async def ensure_tables(target: object, *tables: Table, create: bool = True) -> None:
    """Make sure *tables* exist on *target*'s database (see :func:`framework_engine`), or fail fast.

    With *create*, the missing tables (with their indexes), and the missing indexes of the tables that
    exist, are created first, in one transaction of their own. Processes starting together may all try:
    one that loses a race gets an error from the database (MySQL and MariaDB commit each ``CREATE TABLE``
    on its own, so the others may still be creating the rest), and tries again, skipping what exists, up
    to :data:`CREATE_ATTEMPTS` times. Then every table is checked: it must exist and have every declared
    column, and on PostgreSQL a :class:`UtcTimestamp` column must be ``TIMESTAMP WITH TIME ZONE``. Raises
    :class:`FrameworkSchemaError` naming each problem and how to fix it.
    """
    if not tables:
        return
    engine = framework_engine(target)
    creation_error: DBAPIError | None = None
    if create:
        for attempt in range(1, CREATE_ATTEMPTS + 1):
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(_create, tables)
            except DBAPIError as error:
                creation_error = error
                _logger.debug("framework_tables_creation_failed", extra={"attempt": attempt}, exc_info=True)
                await asyncio.sleep(0.05 * attempt)
            else:
                break
    async with engine.connect() as connection:
        problems = await connection.run_sync(_problems, tables)
    if problems:
        names = ", ".join(table.name for table in tables)
        hint = (
            "Create them with a migration (list pyfly.data.relational.framework_schema.framework_metadata in "
            "Alembic's target_metadata) or let the store create them (pyfly.data.relational.ddl-auto=create)."
        )
        raise FrameworkSchemaError(
            f"The framework tables {names} on {engine.url.render_as_string(hide_password=True)} are not usable: "
            + "; ".join(problems)
            + f". {hint}"
        ) from creation_error
    if creation_error is not None:
        _logger.info("framework_tables_created_concurrently", extra={"tables": [table.name for table in tables]})


def _create(connection: Connection, tables: Sequence[Table]) -> None:
    """Create the missing tables, and the missing indexes of the tables that exist (a table an earlier
    release created without them: the cache's ``expires_at`` index keeps its purge off a full scan)."""
    inspector = inspect(connection)
    existing = [table for table in tables if inspector.has_table(table.name, schema=table.schema)]
    missing = [table for table in tables if table not in existing]
    for metadata in {id(table.metadata): table.metadata for table in missing}.values():
        metadata.create_all(connection, tables=[table for table in missing if table.metadata is metadata])
    for table in existing:
        present = {index["name"] for index in inspector.get_indexes(table.name, schema=table.schema)}
        for index in table.indexes:
            if index.name not in present:
                index.create(connection)


def _problems(connection: Connection, tables: Sequence[Table]) -> list[str]:
    inspector = inspect(connection)
    postgresql = connection.dialect.name == "postgresql"
    problems: list[str] = []
    for table in tables:
        if not inspector.has_table(table.name, schema=table.schema):
            problems.append(f"table {table.name} does not exist")
            continue
        columns = inspector.get_columns(table.name, schema=table.schema)
        reflected = {column["name"].lower(): column for column in columns}
        for column in table.columns:
            found = reflected.get(column.name.lower())
            if found is None:
                problems.append(f"{table.name}.{column.name} does not exist")
            elif postgresql and isinstance(column.type, UtcTimestamp) and not getattr(found["type"], "timezone", True):
                problems.append(
                    f"{table.name}.{column.name} is TIMESTAMP WITHOUT TIME ZONE (convert it: ALTER TABLE {table.name} "
                    f"ALTER COLUMN {column.name} TYPE TIMESTAMPTZ USING {column.name} AT TIME ZONE 'UTC')"
                )
    return problems
