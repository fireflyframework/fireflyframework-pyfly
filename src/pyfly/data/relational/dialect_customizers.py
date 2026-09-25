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
"""What every engine the datasource registry builds gets, by dialect, plus two SPIs.

**SQLite** (:func:`install_sqlite_customizer`). SQLite's defaults, and pysqlite's, are wrong for an
application database: foreign keys are not enforced (orphans commit and ``ON DELETE CASCADE`` does
nothing), and the driver defers ``BEGIN`` until the first write, so the reads of a read-modify-write run
outside the transaction and a concurrent update is lost. Every SQLite connection therefore gets:

- ``PRAGMA foreign_keys=ON`` and a ``busy_timeout``;
- for file databases, ``journal_mode=WAL`` and ``synchronous=NORMAL`` (readers run beside a writer,
  and a commit costs one fsync of the WAL);
- SQLAlchemy's documented pysqlite/aiosqlite recipe: the driver's own transaction handling is turned
  off and the engine emits ``BEGIN`` itself when a transaction starts, so isolation is real. A unit that
  will write asks for ``BEGIN IMMEDIATE`` through :func:`begin_execution_options` and takes the write
  lock up front (waiting ``busy_timeout`` for it) instead of failing on the lock upgrade later.

**Credentials** (:func:`install_credentials_hook`). A ``do_connect`` hook asks a supplier for the user
name and password every time the pool opens a connection, so a rotated password (Vault, a secrets
manager refresh, an IAM token) reaches new connections without a restart.

**After-begin customizers** (:class:`AfterBeginCustomizer`, :func:`run_after_begin`). Code that must
run inside every framework-managed transaction on a datasource, right after ``BEGIN``: a tenant GUC
(``SELECT set_config('app.tenant_id', :tenant, true)``), a ``SET LOCAL statement_timeout``, a
``search_path``. The transaction manager calls :func:`run_after_begin` for every unit it opens, auto
units included.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy import event

from pyfly.config.properties.data import SqliteProperties

if TYPE_CHECKING:
    from sqlalchemy.engine import URL, Connection, Dialect
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
    from sqlalchemy.pool import ConnectionPoolEntry

    from pyfly.data.relational.datasource_registry import DataSource

_logger = logging.getLogger(__name__)

SQLITE_BEGIN_OPTION = "pyfly_sqlite_begin"
"""Connection execution option naming the ``BEGIN`` a SQLite transaction starts with.

``DEFERRED`` (the default: a plain ``BEGIN``), ``IMMEDIATE`` (take the write lock now) or
``EXCLUSIVE``. Other dialects ignore it; :func:`begin_execution_options` sets it for write units.
"""

_BEGIN_MODES = frozenset({"DEFERRED", "IMMEDIATE", "EXCLUSIVE"})


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def is_file_database(url: URL) -> bool:
    """Whether a SQLite *url* names a file (``:memory:`` and ``mode=memory`` URIs do not)."""
    database = url.database or ""
    if database in ("", ":memory:"):
        return False
    if database.startswith("file:") and "mode=memory" in database:
        return False
    return str(url.query.get("mode", "")) != "memory"


def is_autocommit(conn: Connection) -> bool:
    """Whether *conn* runs in ``AUTOCOMMIT``, where SQLAlchemy still fires ``begin`` but no ``BEGIN`` belongs.

    SQLAlchemy answers this with a private helper; when a release no longer has it, the answer comes from
    the public execution options and the engine's ``isolation_level``.
    """
    probe = getattr(conn, "_is_autocommit_isolation", None)
    if callable(probe):
        return bool(probe())
    return autocommit_from_options(conn)


def autocommit_from_options(conn: Connection) -> bool:
    """The fallback of :func:`is_autocommit`: the connection's ``isolation_level`` execution option, or
    else the ``isolation_level`` the engine was created with."""
    level = conn.get_execution_options().get("isolation_level")
    if level is None:
        level = getattr(conn.dialect, "_on_connect_isolation_level", None)
    return str(level or "").upper() == "AUTOCOMMIT"


def begin_execution_options(dialect_name: str, *, read_only: bool) -> dict[str, Any]:
    """Execution options a unit of work applies to its connection before ``BEGIN``.

    A SQLite unit that will write starts with ``BEGIN IMMEDIATE``; everything else needs nothing. Pass
    the result to ``session.connection(execution_options=...)`` (or ``connection.execution_options``)
    before the first statement.
    """
    if dialect_name == "sqlite" and not read_only:
        return {SQLITE_BEGIN_OPTION: "IMMEDIATE"}
    return {}


def install_sqlite_customizer(
    engine: AsyncEngine,
    settings: SqliteProperties,
    *,
    explicit_timeout: bool = False,
) -> None:
    """Apply the SQLite connection setup and the ``BEGIN`` recipe to *engine* (a SQLite engine).

    *explicit_timeout* is true when the URL or the connect arguments set sqlite3's ``timeout``; the
    ``busy_timeout`` PRAGMA then leaves the operator's value alone.
    """
    sync_engine = engine.sync_engine
    file_database = is_file_database(sync_engine.url)

    def _on_connect(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
        # Turn off the driver's transaction handling; _on_begin emits BEGIN itself.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            if settings.foreign_keys:
                cursor.execute("PRAGMA foreign_keys=ON")
            if not explicit_timeout:
                cursor.execute(f"PRAGMA busy_timeout={int(settings.busy_timeout)}")
            if file_database:
                cursor.execute("PRAGMA journal_mode")
                row = cursor.fetchone()
                current = str(row[0]).upper() if row else ""
                if current != settings.journal_mode:
                    # Switching into WAL needs a moment of exclusive access; the mode is stored in the
                    # file, so one connection that manages it is enough.
                    try:
                        cursor.execute(f"PRAGMA journal_mode={settings.journal_mode}")
                    except Exception:  # noqa: BLE001 — a busy database keeps its mode; the next connection retries
                        _logger.warning(
                            "sqlite_journal_mode_not_set",
                            extra={"database": sync_engine.url.database, "journal_mode": settings.journal_mode},
                            exc_info=True,
                        )
                cursor.execute(f"PRAGMA synchronous={settings.synchronous}")
        finally:
            cursor.close()

    def _on_begin(conn: Connection) -> None:
        if is_autocommit(conn):
            return
        # StaticPool (in-memory databases) hands every session the same connection: when another
        # session already opened a transaction on it, this one joins it, as the driver's own handling
        # did, instead of failing with "cannot start a transaction within a transaction".
        if getattr(conn.connection.driver_connection, "in_transaction", False):
            return
        dbapi_connection = conn.connection.dbapi_connection
        # Setting an isolation level (and resetting it when the connection returns to the pool) turns
        # the driver's own BEGIN handling back on; switch it off again before this transaction starts.
        if dbapi_connection is not None and dbapi_connection.isolation_level is not None:
            dbapi_connection.isolation_level = None
        mode = str(conn.get_execution_options().get(SQLITE_BEGIN_OPTION, "DEFERRED")).upper()
        if mode not in _BEGIN_MODES:
            raise ValueError(f"{SQLITE_BEGIN_OPTION} must be one of {sorted(_BEGIN_MODES)}, got {mode!r}")
        conn.exec_driver_sql("BEGIN" if mode == "DEFERRED" else f"BEGIN {mode}")

    event.listen(sync_engine, "connect", _on_connect)
    event.listen(sync_engine, "begin", _on_begin)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

CredentialsSupplier = Callable[[], "tuple[str | None, str | None] | None"]
"""Returns the ``(username, password)`` a new connection must use, or ``None`` to keep the URL's."""


@runtime_checkable
class DataSourceCredentialsProvider(Protocol):
    """Supplies the credentials of new connections, per datasource (IAM tokens, a secrets client).

    A bean implementing it is consulted every time a pool opens a connection, before the live
    configuration; returning ``None`` falls through to the configured URL.

    *datasource* is the datasource's qualified name: ``"primary"``, a named datasource such as
    ``"reporting"``, or ``"<name>.replica"`` for a read replica (``"primary.replica"``). A replica is
    asked for apart from its primary because it usually runs on another host (an IAM token is scoped
    to one) and often logs in as another role; answer ``None`` for it to keep its configured user.
    """

    def datasource_credentials(self, datasource: str) -> tuple[str | None, str | None] | None:
        """The ``(username, password)`` for a new connection to *datasource* (a qualified name), or
        ``None``."""
        ...


def install_credentials_hook(
    engine: AsyncEngine,
    supplier: CredentialsSupplier,
    *,
    explicit_keys: Collection[str] = (),
    on_connect: Callable[[tuple[str | None, str | None], ConnectionPoolEntry], None] | None = None,
) -> None:
    """Make every new connection of *engine* use the credentials *supplier* returns at that moment.

    The engine keeps the URL it was built with; only the user name and password of the connect
    parameters change, recomputed through the dialect so every driver gets its own spelling.
    *explicit_keys* are connect arguments the operator set by hand, which are never overwritten.
    *on_connect* is told the credentials each new connection used, and its pool entry (whose ``info``
    lives as long as that DBAPI connection).

    Only the connect parameters change, so a dialect that takes its credentials from a positional
    connection string (the ODBC dialects, ``mssql+aioodbc``) is not rotated by this hook.
    """
    sync_engine = engine.sync_engine
    base_url = sync_engine.url
    base_credentials = (base_url.username, base_url.password)
    pinned = set(explicit_keys)

    def _do_connect(dialect: Dialect, record: ConnectionPoolEntry, _cargs: list[Any], cparams: dict[str, Any]) -> None:
        credentials = supplier() or base_credentials
        _apply_credentials(dialect, base_url, credentials, cparams, pinned)
        if on_connect is not None:
            on_connect(credentials, record)

    event.listen(sync_engine, "do_connect", _do_connect)


def _apply_credentials(
    dialect: Dialect,
    base_url: URL,
    credentials: tuple[str | None, str | None],
    cparams: dict[str, Any],
    pinned: set[str],
) -> None:
    """Write the connect parameters *credentials* produce into *cparams*.

    SQLAlchemy hands every ``do_connect`` listener the same dictionary, so a change made for one
    connection stays for the next; every URL-derived key is therefore written each time, which also
    restores the original values when the credentials go back to the URL's.
    """
    username, password = credentials
    _, original = dialect.create_connect_args(base_url)
    _, fresh = dialect.create_connect_args(base_url.set(username=username, password=password))
    for key, value in fresh.items():
        if key not in pinned:
            cparams[key] = value
    for key in set(original) - set(fresh) - pinned:
        cparams.pop(key, None)


# ---------------------------------------------------------------------------
# After-begin customizers
# ---------------------------------------------------------------------------


@runtime_checkable
class AfterBeginCustomizer(Protocol):
    """Runs inside every framework-managed transaction on a datasource, right after ``BEGIN``.

    Declare it as a bean and it applies to every datasource; give the class a ``datasources``
    attribute (a collection of names) to limit it, or register it for one datasource with
    ``DataSourceRegistry.add_customizer(customizer, datasource="name")``. *connection* is the unit's
    ``AsyncSession`` or ``AsyncConnection``: execute through it, and the statement runs in the unit's
    transaction. An exception aborts the unit (it rolls back).

    Example, a tenant GUC read from a ``ContextVar`` and skipped when unset::

        @component
        class TenantGuc:
            async def after_begin(self, connection, datasource):
                tenant = current_tenant.get()
                if tenant is not None and datasource.capabilities.dialect == "postgresql":
                    await connection.execute(
                        text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant}
                    )
    """

    async def after_begin(self, connection: AsyncSession | AsyncConnection, datasource: DataSource) -> None:
        """Customize the transaction that just began on *datasource*."""
        ...


def customizer_datasources(customizer: object) -> frozenset[str] | None:
    """The datasource names a customizer bean limits itself to (its ``datasources`` attribute), or
    ``None`` for all of them."""
    names = getattr(customizer, "datasources", None)
    if names is None:
        return None
    if isinstance(names, str):
        return frozenset({names})
    if isinstance(names, (Collection, Mapping)):
        return frozenset(str(name) for name in names)
    raise TypeError(f"{type(customizer).__name__}.datasources must be a collection of datasource names")


async def run_after_begin(datasource: DataSource, connection: AsyncSession | AsyncConnection) -> None:
    """Run *datasource*'s after-begin customizers on *connection*, in order.

    The transaction manager calls this right after it begins a unit of work (its own transactions and
    the short auto units repositories open), after the isolation level is applied and before the first
    statement of the unit. It is a no-op when the datasource has no customizer.
    """
    for customizer in datasource.customizers:
        await customizer.after_begin(connection, datasource)
