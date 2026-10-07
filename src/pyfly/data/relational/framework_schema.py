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

Every table PyFly keeps in an application's database (``pyfly_*`` and the shared ``firefly_*`` tables) is a
:class:`~sqlalchemy.Table` on :data:`framework_metadata`, with column types that work on every backend:

- bounded :func:`key_string` keys (MySQL and MariaDB cannot index an unbounded ``TEXT`` column), compared
  exactly on every backend (a binary collation on MySQL and MariaDB);
- :class:`UtcTimestamp` instants: UTC with microseconds everywhere, aware in Python;
- :func:`long_text` and :func:`long_binary` payloads (``LONGTEXT``/``LONGBLOB`` on MySQL and MariaDB,
  whose ``TEXT``/``BLOB`` stop at 64 KiB);
- a naming convention, so constraints and indexes get the same name on every backend.

Alembic sees them through the metadata. An ``env.py`` whose ``target_metadata`` lists the application's
metadata and this one (``target_metadata = [Base.metadata, framework_metadata]``) autogenerates their
``CREATE TABLE`` and never a ``DROP TABLE`` for them.

The tables, and who uses them:

================================  ============================================================================
Table                             Used by
================================  ============================================================================
``pyfly_orchestration_state``     ``SqlAlchemyPersistenceProvider`` (saga, TCC and workflow state)
``pyfly_cache_entries``           ``PostgresCacheAdapter`` (``pyfly.cache.provider=postgres``)
``pyfly_locks``                   ``LeaseLock`` (``@scheduled(lock=...)`` and other leases)
``pyfly_users``                   ``SqlUserDetailsService``
``pyfly_event_store``             ``SqlAlchemyEventStore`` (the event log, paged by global position)
``pyfly_event_store_head``        ``SqlAlchemyEventStore`` (the last global position given out)
``pyfly_snapshots``               ``SqlAlchemySnapshotStore``
``pyfly_projection_checkpoints``  ``SqlAlchemyCheckpointStore`` (the position of each projection)
``pyfly_outbox_events``           The transactional outbox (``pyfly.eda.outbox``): the event bus of the
                                  ``postgres`` and ``database`` providers, ``TransactionalOutbox``
``pyfly_outbox_deliveries``       The outbox: one row per consumer group an event is still owed to
``pyfly_outbox_consumers``        The outbox: the consumer groups, and the destinations each consumes
``pyfly_outbox_dead_letters``     The outbox: the deliveries that failed on every attempt
``pyfly_oauth2_grants``           ``PostgresTokenStore``: refresh tokens, authorization codes, pushed requests
``pyfly_oauth2_token_families``   ``PostgresTokenStore``: the refresh-token rotation families
``pyfly_sessions``                ``SqlSessionStore`` (``pyfly.session.store=postgres``)
``pyfly_session_registrations``   ``PostgresSessionRegistry``: the sessions of each principal
``pyfly_session_principals``      ``PostgresSessionRegistry``: a row per principal, locked by a login
``firefly_feature_flags``         ``SqlAlchemyFlagStore``: the writable feature-flag layer, shared with LaraFly
``firefly_feature_flag_changes``  ``SqlAlchemyFlagStore``: one row per write; the highest ``id`` is the revision
================================  ============================================================================

A store that is configured with another table name declares that table here too, through the table's
factory function (:func:`orchestration_state_table` and the others), so a migration environment that
builds the store's table the same way sees it.

Stores call :func:`ensure_tables` when they start: it creates their tables when the schema strategy
allows it (:func:`creates_tables`: ``pyfly.data.relational.ddl-auto`` is ``create`` or ``create-drop``;
with ``none``, ``validate`` or any other value the tables are left to migrations), then checks
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
    Boolean,
    Column,
    DateTime,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Unicode,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.dialects import mssql, mysql
from sqlalchemy.engine import Connection, Dialect
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateIndex
from sqlalchemy.types import TypeDecorator, TypeEngine

if TYPE_CHECKING:
    from pyfly.container.container import Container
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry

__all__ = [
    "CACHE_ENTRIES",
    "CREATE_ATTEMPTS",
    "EVENT_STORE",
    "EVENT_STORE_HEAD",
    "FEATURE_FLAG_CHANGES",
    "FEATURE_FLAGS",
    "FRAMEWORK_TABLE_PREFIX",
    "LOCKS",
    "NAMING_CONVENTION",
    "OAUTH2_GRANTS",
    "OAUTH2_TOKEN_FAMILIES",
    "ORCHESTRATION_STATE",
    "OUTBOX_CONSUMERS",
    "OUTBOX_DEAD_LETTERS",
    "OUTBOX_DELIVERIES",
    "OUTBOX_EVENTS",
    "PROJECTION_CHECKPOINTS",
    "SESSIONS",
    "SESSION_PRINCIPALS",
    "SESSION_REGISTRATIONS",
    "SNAPSHOTS",
    "USERS",
    "FrameworkSchemaError",
    "KeyString",
    "UtcTimestamp",
    "ZonelessUtcTimestamp",
    "cache_entries",
    "cache_entries_table",
    "context_datasource_registry",
    "creates_tables",
    "ensure_tables",
    "event_store",
    "event_store_head",
    "event_store_head_table",
    "event_store_table",
    "feature_flag_changes",
    "feature_flag_changes_table",
    "feature_flags",
    "feature_flags_table",
    "framework_engine",
    "framework_metadata",
    "key_string",
    "locks",
    "locks_table",
    "long_binary",
    "long_text",
    "module_datasource",
    "oauth2_grants",
    "oauth2_grants_table",
    "oauth2_token_families",
    "oauth2_token_families_table",
    "orchestration_state",
    "orchestration_state_table",
    "outbox_consumers",
    "outbox_consumers_table",
    "outbox_dead_letters",
    "outbox_dead_letters_table",
    "outbox_deliveries",
    "outbox_deliveries_table",
    "outbox_events",
    "outbox_events_table",
    "projection_checkpoints",
    "projection_checkpoints_table",
    "session_principals",
    "session_principals_table",
    "session_registrations",
    "session_registrations_table",
    "sessions",
    "sessions_table",
    "snapshots",
    "snapshots_table",
    "users",
    "users_table",
]

_logger = logging.getLogger(__name__)

FRAMEWORK_TABLE_PREFIX = "pyfly_"
"""The prefix of every table the framework declares under its default name, except the two feature-flag tables
(``firefly_feature_flags``, ``firefly_feature_flag_changes``), whose names are shared with LaraFly."""

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
"""The ``MetaData`` of framework tables (``pyfly_*`` and shared ``firefly_*``); list it in Alembic's
``target_metadata``."""

ORCHESTRATION_STATE = "pyfly_orchestration_state"
CACHE_ENTRIES = "pyfly_cache_entries"
LOCKS = "pyfly_locks"
USERS = "pyfly_users"
EVENT_STORE = "pyfly_event_store"
EVENT_STORE_HEAD = "pyfly_event_store_head"
SNAPSHOTS = "pyfly_snapshots"
PROJECTION_CHECKPOINTS = "pyfly_projection_checkpoints"
OUTBOX_EVENTS = "pyfly_outbox_events"
OUTBOX_DELIVERIES = "pyfly_outbox_deliveries"
OUTBOX_CONSUMERS = "pyfly_outbox_consumers"
OUTBOX_DEAD_LETTERS = "pyfly_outbox_dead_letters"
OAUTH2_GRANTS = "pyfly_oauth2_grants"
OAUTH2_TOKEN_FAMILIES = "pyfly_oauth2_token_families"
SESSIONS = "pyfly_sessions"
SESSION_REGISTRATIONS = "pyfly_session_registrations"
SESSION_PRINCIPALS = "pyfly_session_principals"
FEATURE_FLAGS = "firefly_feature_flags"
FEATURE_FLAG_CHANGES = "firefly_feature_flag_changes"

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


class ZonelessUtcTimestamp(TypeDecorator[datetime]):
    """A UTC instant stored without a time zone, compatible with LaraFly's feature-flag migration."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"ZonelessUtcTimestamp takes a datetime, got {type(value).__name__}")
        instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return instant.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class KeyString(TypeDecorator[str]):
    """A key: Unicode text of at most *length* characters that compares exactly, as on PostgreSQL and SQLite.

    - MySQL and MariaDB: ``VARCHAR(length) CHARACTER SET utf8mb4`` with a binary collation that does not pad
      (``utf8mb4_0900_bin`` on MySQL 8, ``utf8mb4_nopad_bin`` on MariaDB, ``utf8mb4_bin`` on older servers).
      Their default collations ignore case and accents (and MariaDB's trailing spaces), so ``User:1`` and
      ``user:1`` would be one cache entry, one lease, one user.
    - SQL Server: ``NVARCHAR`` (its ``VARCHAR`` is not Unicode); elsewhere ``VARCHAR(length)``.
    """

    impl = Unicode
    cache_ok = True

    def __init__(self, length: int = 255) -> None:
        super().__init__(length=length)
        self.length = length

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(
                mysql.VARCHAR(self.length, charset="utf8mb4", collation=_exact_collation(dialect))
            )
        return dialect.type_descriptor(Unicode(self.length))


def _exact_collation(dialect: Dialect) -> str:
    """The binary collation of *dialect*'s server that also compares trailing spaces (a ``mysql://`` URL on a
    MariaDB server included: the dialect knows it is MariaDB once connected)."""
    version = tuple(getattr(dialect, "server_version_info", None) or ())
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        return "utf8mb4_nopad_bin" if not version or version >= (10, 2, 2) else "utf8mb4_bin"
    return "utf8mb4_0900_bin" if not version or version >= (8, 0, 1) else "utf8mb4_bin"


def key_string(length: int = 255) -> TypeEngine[str]:
    """A key column (:class:`KeyString`): ``VARCHAR(length)`` compared exactly on every backend."""
    return KeyString(length)


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


def event_store_table(name: str = EVENT_STORE) -> Table:
    """The event log of ``SqlAlchemyEventStore``: one row per event, unique per ``(aggregate_id, sequence)``.

    ``payload`` is the envelope's JSON (what the store reads back) and ``metadata`` its metadata's JSON; the
    other columns are what the queries filter on. ``recorded_at`` is when the database recorded the event, by
    its own clock. ``global_position`` places the event on the global stream (``SqlAlchemyEventStore`` documents
    the order each strategy gives): with the ``head-row`` strategy the store gives it to the event once the event
    has committed (it is ``NULL`` until then), with ``xid8`` as the event is inserted; projections page by it,
    through its unique index. On SQL Server the index skips ``NULL``, which it would otherwise count as a duplicate.
    """

    def build(table_name: str) -> Table:
        table = Table(
            table_name,
            framework_metadata,
            Column("event_id", key_string(64), primary_key=True),
            Column("aggregate_id", key_string(), nullable=False),
            Column("aggregate_type", key_string(), nullable=False),
            Column("sequence", Integer(), nullable=False),
            Column("event_type", key_string(), nullable=False),
            Column("payload", long_text(), nullable=False),
            Column("metadata", long_text(), nullable=False),
            Column("occurred_at", UtcTimestamp(), nullable=False),
            Column("version", Integer(), nullable=False),
            Column("tenant_id", key_string(64), nullable=True),
            Column("recorded_at", UtcTimestamp(), nullable=True),
            Column("global_position", BigInteger(), nullable=True),
            UniqueConstraint("aggregate_id", "sequence"),
        )
        position = table.c.global_position
        Index(None, position, unique=True, mssql_where=position.is_not(None))
        return table

    return _declare("event_store", name, build)


def event_store_head_table(name: str = EVENT_STORE_HEAD) -> Table:
    """The last global position given out, one row per event table (``store``), and the strategy that event
    table's positions follow (``head-row`` or ``xid8``, recorded by the first store that started on it). The
    stores that give out positions lock the row while they do, so they give them out one at a time."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("store", key_string(), primary_key=True),
            Column("position", BigInteger(), nullable=False),
            Column("strategy", key_string(32), nullable=False),
        )

    return _declare("event_store_head", name, build)


def snapshots_table(name: str = SNAPSHOTS) -> Table:
    """The latest snapshot of each aggregate: its state (``payload``, JSON) as of event ``sequence``."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("aggregate_id", key_string(), primary_key=True),
            Column("aggregate_type", key_string(), nullable=False),
            Column("sequence", Integer(), nullable=False),
            Column("payload", long_text(), nullable=False),
            Column("created_at", UtcTimestamp(), nullable=False),
        )

    return _declare("snapshots", name, build)


def projection_checkpoints_table(name: str = PROJECTION_CHECKPOINTS) -> Table:
    """The global position each projection has applied up to, written in the unit of work of the batch that
    reached it."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("projection", key_string(), primary_key=True),
            Column("position", BigInteger(), nullable=False),
            Column("updated_at", UtcTimestamp(), nullable=False),
        )

    return _declare("projection_checkpoints", name, build)


def outbox_events_table(name: str = OUTBOX_EVENTS) -> Table:
    """The transactional outbox: one row per published event, written in the publisher's unit of work.

    ``id`` is the outbox's own key, which the deliveries refer to; ``event_id`` is the envelope's id, the one
    a consumer deduplicates on. ``payload`` and ``headers`` are JSON text.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("id", BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True),
            Column("event_id", key_string(64), nullable=False),
            Column("destination", key_string(), nullable=False),
            Column("event_type", key_string(), nullable=False),
            Column("payload", long_text(), nullable=False),
            Column("headers", long_text(), nullable=False),
            Column("created_at", UtcTimestamp(), nullable=False, index=True),
        )

    return _declare("outbox_events", name, build)


def outbox_deliveries_table(name: str = OUTBOX_DELIVERIES) -> Table:
    """What the outbox still owes: one row per consumer group and event, deleted once the group handled it.

    A row is claimed when ``available_at`` has passed: the claim moves it a lease ahead and records the claim
    in ``claimed_by``, a failure moves it to the next attempt's time. ``done`` lists (JSON) the subscriptions
    of the group that handled the event already, ``last_error`` is the last failure.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("consumer_group", key_string(), primary_key=True),
            Column("outbox_id", BigInteger(), primary_key=True, index=True),
            Column("available_at", UtcTimestamp(), nullable=False),
            Column("attempts", Integer(), nullable=False),
            Column("claimed_by", key_string(), nullable=True),
            Column("done", long_text(), nullable=True),
            Column("last_error", long_text(), nullable=True),
            Index(None, "consumer_group", "available_at"),
        )

    return _declare("outbox_deliveries", name, build)


def outbox_consumers_table(name: str = OUTBOX_CONSUMERS) -> Table:
    """The consumer groups of the outbox, one row per destination a group consumes (``*``: every one): a
    published event gets a delivery row for each group registered for its destination."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("consumer_group", key_string(), primary_key=True),
            Column("destination", key_string(), primary_key=True),
            Column("registered_at", UtcTimestamp(), nullable=False),
        )

    return _declare("outbox_consumers", name, build)


def outbox_dead_letters_table(name: str = OUTBOX_DEAD_LETTERS) -> Table:
    """The events a subscription failed to handle on every attempt, with a copy of the event (the outbox
    row may be pruned since) and the last failure. Kept until someone deletes them."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("id", key_string(64), primary_key=True),
            Column("consumer_group", key_string(), nullable=True, index=True),
            Column("subscription", long_text(), nullable=True),
            Column("event_id", key_string(64), nullable=False),
            Column("destination", key_string(), nullable=False),
            Column("event_type", key_string(), nullable=False),
            Column("payload", long_text(), nullable=False),
            Column("headers", long_text(), nullable=False),
            Column("occurred_at", UtcTimestamp(), nullable=False),
            Column("error_type", key_string(), nullable=False),
            Column("error_message", long_text(), nullable=False),
            Column("attempts", Integer(), nullable=False),
            Column("failed_at", UtcTimestamp(), nullable=False, index=True),
        )

    return _declare("outbox_dead_letters", name, build)


def oauth2_grants_table(name: str = OAUTH2_GRANTS) -> Table:
    """The table of the SQL OAuth2 token store: refresh tokens, authorization codes and pushed authorization
    requests, one row each (``kind`` says which).

    The columns a grant decides on are typed, so each grant is a conditional statement: ``used`` (a code or a
    refresh token is consumed with ``UPDATE ... WHERE used = false``), ``expires_at`` (indexed, for the purge)
    and ``family_id`` (a refresh token's rotation family, and the family a code issued). A family's tokens are
    deleted with one statement through the ``(family_id, kind)`` index, which never reaches the row of the code
    that issued the family (MySQL and MariaDB lock every row a deletion scans: a late redemption of that code,
    holding its row, would deadlock with the revocation). ``data`` is the rest of the record as JSON (scope,
    user, PKCE challenge...).
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("token_id", key_string(), primary_key=True),
            Column("kind", key_string(32), nullable=False),
            Column("client_id", key_string(), nullable=False),
            Column("family_id", key_string(), nullable=True),
            Column("used", Boolean(), nullable=False, default=False),
            Column("expires_at", UtcTimestamp(), nullable=False, index=True),
            Column("data", long_text(), nullable=False),
            Index(None, "family_id", "kind"),
        )

    return _declare("oauth2_grants", name, build)


def oauth2_token_families_table(name: str = OAUTH2_TOKEN_FAMILIES) -> Table:
    """The refresh-token rotation families of the SQL OAuth2 token store, one row each.

    ``active`` only ever goes from true to false (a revocation is ``UPDATE ... SET active = false``), and a
    rotation locks the row and mints its token only while it is true. ``expires_at`` is the expiry of the
    family's latest token.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("family_id", key_string(), primary_key=True),
            Column("client_id", key_string(), nullable=False),
            Column("active", Boolean(), nullable=False, default=True),
            Column("expires_at", UtcTimestamp(), nullable=False, index=True),
        )

    return _declare("oauth2_token_families", name, build)


def sessions_table(name: str = SESSIONS) -> Table:
    """The table of the SQL session store: each HTTP session's attributes as JSON, and when it expires."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("session_id", key_string(), primary_key=True),
            Column("data", long_text(), nullable=False),
            Column("expires_at", UtcTimestamp(), nullable=False, index=True),
        )

    return _declare("sessions", name, build)


def session_registrations_table(name: str = SESSION_REGISTRATIONS) -> Table:
    """The sessions of each principal, for the session concurrency cap (``PostgresSessionRegistry``).

    ``expires_at`` (indexed) is when the registry next checks that the session is still alive: the purge
    drops the registration of a session the session store no longer has, and renews the others.
    """

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("session_id", key_string(), primary_key=True),
            Column("principal", key_string(), nullable=False, index=True),
            Column("created_at", UtcTimestamp(), nullable=False),
            Column("expires_at", UtcTimestamp(), nullable=False, index=True),
        )

    return _declare("session_registrations", name, build)


def session_principals_table(name: str = SESSION_PRINCIPALS) -> Table:
    """One row per principal that logged in under a session cap: a login locks it (``UPDATE ... SET version =
    version + 1``) while it counts and registers the principal's sessions, so concurrent logins of one
    principal, on any instance, take turns."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("principal", key_string(), primary_key=True),
            Column("version", BigInteger(), nullable=False, default=0),
        )

    return _declare("session_principals", name, build)


def feature_flags_table(name: str = FEATURE_FLAGS) -> Table:
    """One writable flagd definition per key, with its version and latest writer."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("flag_key", key_string(128), primary_key=True),
            Column("definition", long_text(), nullable=False),
            Column("version", Integer(), nullable=False),
            Column("updated_at", ZonelessUtcTimestamp(), nullable=False),
            Column("updated_by", key_string(255), nullable=True),
        )

    return _declare("feature_flags", name, build)


def feature_flag_changes_table(name: str = FEATURE_FLAG_CHANGES) -> Table:
    """One audit row per write; the highest id is the store revision."""

    def build(table_name: str) -> Table:
        return Table(
            table_name,
            framework_metadata,
            Column("id", BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True),
            Column("flag_key", key_string(128), nullable=False),
            Column("action", key_string(16), nullable=False),
            Column("definition", long_text(), nullable=True),
            Column("previous", long_text(), nullable=True),
            Column("actor", key_string(255), nullable=True),
            Column("changed_at", ZonelessUtcTimestamp(), nullable=False),
            Index(f"{table_name}_key", "flag_key", "id"),
        )

    return _declare("feature_flag_changes", name, build)


orchestration_state = orchestration_state_table()
"""``pyfly_orchestration_state`` (:func:`orchestration_state_table`)."""

cache_entries = cache_entries_table()
"""``pyfly_cache_entries`` (:func:`cache_entries_table`)."""

locks = locks_table()
"""``pyfly_locks`` (:func:`locks_table`)."""

users = users_table()
"""``pyfly_users`` (:func:`users_table`)."""

event_store = event_store_table()
"""``pyfly_event_store`` (:func:`event_store_table`)."""

event_store_head = event_store_head_table()
"""``pyfly_event_store_head`` (:func:`event_store_head_table`)."""

snapshots = snapshots_table()
"""``pyfly_snapshots`` (:func:`snapshots_table`)."""

projection_checkpoints = projection_checkpoints_table()
"""``pyfly_projection_checkpoints`` (:func:`projection_checkpoints_table`)."""

outbox_events = outbox_events_table()
"""``pyfly_outbox_events`` (:func:`outbox_events_table`)."""

outbox_deliveries = outbox_deliveries_table()
"""``pyfly_outbox_deliveries`` (:func:`outbox_deliveries_table`)."""

outbox_consumers = outbox_consumers_table()
"""``pyfly_outbox_consumers`` (:func:`outbox_consumers_table`)."""

outbox_dead_letters = outbox_dead_letters_table()
"""``pyfly_outbox_dead_letters`` (:func:`outbox_dead_letters_table`)."""

oauth2_grants = oauth2_grants_table()
"""``pyfly_oauth2_grants`` (:func:`oauth2_grants_table`)."""

oauth2_token_families = oauth2_token_families_table()
"""``pyfly_oauth2_token_families`` (:func:`oauth2_token_families_table`)."""

sessions = sessions_table()
"""``pyfly_sessions`` (:func:`sessions_table`)."""

session_registrations = session_registrations_table()
"""``pyfly_session_registrations`` (:func:`session_registrations_table`)."""

session_principals = session_principals_table()
"""``pyfly_session_principals`` (:func:`session_principals_table`)."""

feature_flags = feature_flags_table()
"""``firefly_feature_flags`` (:func:`feature_flags_table`)."""

feature_flag_changes = feature_flag_changes_table()
"""``firefly_feature_flag_changes`` (:func:`feature_flag_changes_table`)."""


# ---------------------------------------------------------------------------------------------------------
# Creating and verifying the tables of a store
# ---------------------------------------------------------------------------------------------------------


class FrameworkSchemaError(RuntimeError):
    """A framework table a store needs is missing or does not have the expected columns."""


CREATE_ATTEMPTS = 5
"""How many times :func:`ensure_tables` tries to create the missing tables while other processes race it."""


_CREATING_STRATEGIES = frozenset({"create", "create-drop"})


def creates_tables(ddl_auto: str | None) -> bool:
    """Whether a store creates its missing framework tables when it starts, under the schema strategy
    *ddl_auto* (the effective ``pyfly.data.relational.ddl-auto``): ``create`` and ``create-drop`` do (the
    framework tables are never dropped); ``none``, ``validate`` and any other value leave the schema to
    migrations, and the store only checks it. ``update`` is no strategy a store is given
    (:func:`~pyfly.config.properties.data.ddl_auto_strategy` refuses it)."""
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
    exist, are created first, in one transaction of their own. On PostgreSQL a missing index of a table that
    exists is built afterwards with ``CREATE INDEX CONCURRENTLY`` on an autocommit connection: a plain
    ``CREATE INDEX`` would hold up every write to the table (those of the nodes still running the earlier
    release during a rolling deploy) for as long as the build takes. Processes starting together may all try:
    one that loses a race gets an error from the database (MySQL and MariaDB commit each ``CREATE TABLE``
    on its own, so the others may still be creating the rest), and tries again, skipping what exists, up
    to :data:`CREATE_ATTEMPTS` times. Then every table is checked: it must exist and have every declared
    column, and on PostgreSQL a :class:`UtcTimestamp` column must be ``TIMESTAMP WITH TIME ZONE``. Raises
    :class:`FrameworkSchemaError` naming each problem and how to fix it.

    Indexes only speed the stores up, so a problem with one is logged, not raised: when every attempt failed
    a WARNING (``framework_schema_changes_failed``) carries the last error, a declared index that is missing
    gets a WARNING (``framework_index_missing``) with the statement that creates it, and on PostgreSQL an
    index a failed or interrupted concurrent build left invalid, which the server does not use and
    ``IF NOT EXISTS`` skips, gets one (``framework_index_invalid``) with the ``REINDEX`` that rebuilds it.
    """
    if not tables:
        return
    engine = framework_engine(target)
    creation_error: DBAPIError | None = None
    created = False
    if create:
        for attempt in range(1, CREATE_ATTEMPTS + 1):
            try:
                async with engine.begin() as connection:
                    deferred = await connection.run_sync(_create, tables)
                if deferred:
                    await _create_indexes_concurrently(engine, deferred)
            except DBAPIError as error:
                creation_error = error
                _logger.debug("framework_tables_creation_failed", extra={"attempt": attempt}, exc_info=True)
                await asyncio.sleep(0.05 * attempt)
            else:
                created = True
                break
    async with engine.connect() as connection:
        problems = await connection.run_sync(_problems, tables)
        index_problems = [] if problems else await connection.run_sync(_index_problems, tables, create)
    names = [table.name for table in tables]
    if problems:
        hint = (
            "Create them with a migration (list pyfly.data.relational.framework_schema.framework_metadata in "
            "Alembic's target_metadata) or let the store create them (pyfly.data.relational.ddl-auto=create)."
        )
        where = engine.url.render_as_string(hide_password=True)
        raise FrameworkSchemaError(
            f"The framework tables {', '.join(names)} on {where} are not usable: " + "; ".join(problems) + f". {hint}"
        ) from creation_error
    if create and not created:
        _logger.warning(
            "framework_schema_changes_failed",
            extra={"tables": names, "attempts": CREATE_ATTEMPTS},
            exc_info=creation_error,
        )
    elif creation_error is not None:
        _logger.info("framework_tables_created_concurrently", extra={"tables": names})
    for event, details in index_problems:
        _logger.warning(event, extra=details)


def _create(connection: Connection, tables: Sequence[Table]) -> list[Index]:
    """Create the missing tables, and the missing indexes of the tables that exist (a table an earlier
    release created without them: the cache's ``expires_at`` index keeps its purge off a full scan).

    On PostgreSQL the missing indexes of existing tables are returned instead, for
    :func:`_create_indexes_concurrently`."""
    inspector = inspect(connection)
    existing = [table for table in tables if inspector.has_table(table.name, schema=table.schema)]
    missing = [table for table in tables if table not in existing]
    for metadata in {id(table.metadata): table.metadata for table in missing}.values():
        metadata.create_all(connection, tables=[table for table in missing if table.metadata is metadata])
    deferred: list[Index] = []
    for table in existing:
        present = {index["name"] for index in inspector.get_indexes(table.name, schema=table.schema)}
        for index in table.indexes:
            if index.name in present:
                continue
            if connection.dialect.name == "postgresql":
                deferred.append(index)
            else:
                index.create(connection)
    return deferred


_CREATE_INDEX = re.compile(r"^CREATE (UNIQUE )?INDEX ")


async def _create_indexes_concurrently(engine: AsyncEngine, indexes: Sequence[Index]) -> None:
    """Build *indexes* on PostgreSQL with ``CREATE INDEX CONCURRENTLY IF NOT EXISTS``, each in autocommit (a
    concurrent build cannot run in a transaction). A node that finds another node's build under way skips it."""
    async with engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        for index in indexes:
            ddl = str(CreateIndex(index, if_not_exists=True).compile(dialect=autocommit.dialect))
            await autocommit.execute(text(_CREATE_INDEX.sub(r"CREATE \1INDEX CONCURRENTLY ", ddl, count=1)))
            _logger.info("framework_index_created", extra={"index": index.name})


_INVALID_INDEXES = text(
    "SELECT ic.relname FROM pg_catalog.pg_index i "
    "JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_catalog.pg_class tc ON tc.oid = i.indrelid "
    "JOIN pg_catalog.pg_namespace n ON n.oid = tc.relnamespace "
    "WHERE NOT i.indisvalid AND tc.relname = :table AND n.nspname = :schema"
)


def _index_problems(connection: Connection, tables: Sequence[Table], create: bool) -> list[tuple[str, dict[str, str]]]:
    """The declared indexes of *tables* (which exist) that are missing, or on PostgreSQL invalid: the event to
    log for each, and its details."""
    inspector = inspect(connection)
    postgresql = connection.dialect.name == "postgresql"
    found: list[tuple[str, dict[str, str]]] = []
    for table in tables:
        if not table.indexes:
            continue
        present = {str(index["name"]).lower() for index in inspector.get_indexes(table.name, schema=table.schema)}
        invalid: set[str] = set()
        if postgresql:
            schema = table.schema or inspector.default_schema_name
            rows = connection.execute(_INVALID_INDEXES, {"table": table.name, "schema": schema})
            invalid = {str(row[0]).lower() for row in rows}
        for index in sorted(table.indexes, key=lambda declared: str(declared.name)):
            name = str(index.name)
            if name.lower() in invalid:
                hint = (
                    "a concurrent build of it failed or was interrupted, so PostgreSQL does not use it: rebuild it "
                    f"with REINDEX INDEX CONCURRENTLY {name}"
                )
                found.append(("framework_index_invalid", {"index": name, "table": table.name, "hint": hint}))
            elif name.lower() not in present:
                if create:
                    reason = "it could not be built (framework_schema_changes_failed says why)"
                else:
                    reason = "the schema is left to migrations (pyfly.data.relational.ddl-auto)"
                ddl = str(CreateIndex(index).compile(dialect=connection.dialect)).strip()
                hint = f"{reason}: create it with a migration or run {ddl}"
                found.append(("framework_index_missing", {"index": name, "table": table.name, "hint": hint}))
    return found


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
