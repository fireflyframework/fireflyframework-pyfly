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
"""The SQLAlchemy transaction manager: units of work on one datasource of the ``DataSourceRegistry``.

One :class:`SqlAlchemyTransactionManager` serves one datasource (``for_datasource``); a session factory
that no registry owns gets an ad-hoc manager of its own (``for_sessionmaker``). The relational
auto-configuration serves the ``primary`` datasource on the application's primary session factory bean
(:func:`bind_primary_session_factory`): the registry's own, or the ``async_sessionmaker``, ``AsyncEngine``
or ``DataSourceRegistry`` bean that replaced it. Every unit it opens gets a fresh
:class:`~pyfly.data.relational.sqlalchemy.session.UnitSession` and, before its first statement:

- the datasource's begin options (``BEGIN IMMEDIATE`` for a SQLite unit that will write);
- the isolation level, through ``session.connection(execution_options={"isolation_level": ...})``,
  validated against the dialect (asyncpg has no ``READ UNCOMMITTED``; SQLite has only ``SERIALIZABLE`` and
  ``READ UNCOMMITTED``);
- for a read-only unit: the datasource's replica when one is configured, ``session.info["read_only"]``,
  an ORM ``before_flush`` guard that refuses writes, and the dialect hint (``BEGIN READ ONLY`` on
  PostgreSQL, ``SET TRANSACTION READ ONLY`` on MySQL and MariaDB);
- the datasource's after-begin customizers (a tenant GUC), right after ``BEGIN``;
- for a unit with a timeout on PostgreSQL, ``SET LOCAL statement_timeout``, so a stuck statement is
  cancelled on the server too.

Auto units (a repository call outside a transaction) use the same path, with one difference: their sessions
never expire on commit, whatever the session factory says, since what the call returns outlives the unit. A
read auto unit on a dialect with
fast autocommit reads (PostgreSQL) runs on an ``AUTOCOMMIT`` connection, one round trip instead of three,
unless the datasource has after-begin customizers: their transaction-local settings would not outlive
their own statement there. A write auto unit commits.

A streamed result still open when a unit completes is closed before its ``COMMIT`` or ``ROLLBACK`` on a
dialect whose connection has one active result at a time (MySQL, MariaDB), and so is one still open when the
savepoint it was opened in ends: nothing else can run on that connection until it is.

A commit whose connection fails while ``COMMIT`` is in flight raises
:class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`. A unit whose operation was cancelled in
flight (the unit is *poisoned*) has its connection invalidated instead of rolled back, so the pool never
gets back a connection in an unknown state. On SQLite the discarded connection rolls back on aiosqlite's
worker thread first, and a statement still running there is interrupted
(:mod:`~pyfly.data.relational.sqlalchemy.sqlite_discard`): a cancelled unit never leaves the write lock
held by a half-closed handle.

SQLite has one writer. A new write unit that would wait for the write lock of a unit the same task keeps
open (it suspended it with ``REQUIRES_NEW`` or ``NOT_SUPPORTED``) fails at once with
:class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` instead of waiting ``busy_timeout``
for itself. A child task (``asyncio.gather``) that opens a write unit of its own while its parent's write
unit is open is not refused: the manager cannot tell a parent that awaits it from one that commits
meanwhile, so it waits ``busy_timeout`` and fails with ``database is locked`` when the parent awaits it.

An in-memory SQLite database lives on one connection (``StaticPool``) that every session shares, so two
units cannot overlap on it: a unit that begins while another one holds the connection (a concurrent
request, ``REQUIRES_NEW``, a repository call while ``stream_all`` iterates outside a transaction) fails at
once with ``IllegalTransactionStateError`` instead of sharing, or breaking, the other's transaction. A
poisoned unit there rolls back instead of discarding the connection, which would drop the database.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from sqlalchemy.orm import Session, SessionTransaction
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from pyfly.data.relational.datasource_registry import (
    DataSource,
    DataSourceCapabilities,
    DataSourceConfigurationError,
    DataSourceRegistry,
    NoSuchDataSourceError,
    datasource_of,
)
from pyfly.data.relational.dialect_customizers import begin_execution_options, is_file_database
from pyfly.data.relational.sqlalchemy import sqlite_discard
from pyfly.data.relational.sqlalchemy.session import (
    RELEASING_SAVEPOINT,
    UnitSession,
    close_open_stream,
    track_savepoint,
    unit_session_class,
)
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import CommitOutcomeUnknownError, IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.registry import (
    PRIMARY,
    TransactionManagerRegistry,
    installed_registry,
    register_resource_resolver,
)
from pyfly.data.transaction.template import run_shielded, shield_scope
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = [
    "SqlAlchemyTransactionManager",
    "bind_primary_session_factory",
    "transaction_managers_for",
    "unbind_primary_session_factory",
]

_logger = logging.getLogger(__name__)

_AUTOCOMMIT = "pyfly_autocommit"
"""``UnitOfWork.attributes`` key: the unit runs on an ``AUTOCOMMIT`` connection."""

_DRIVER_CONNECTION = "pyfly_driver_connection"
"""``UnitOfWork.attributes`` key: the driver connection of a SQLite unit, whose write lock a discard releases."""

_TEMPLATE_SAVEPOINTS = "pyfly_template_savepoints"
"""``UnitOfWork.attributes`` key: the savepoints of the unit's ``Propagation.NESTED`` scopes, with their depth."""

_SAVEPOINT_FAILURES = "pyfly_savepoint_failures"
"""``UnitOfWork.attributes`` key: statements that failed inside savepoints the application opened, until those
savepoints roll back (see ``marks_rollback_only``)."""

_SHARED_CONNECTION = "pyfly_shared_connection"
"""``UnitOfWork.attributes`` key: the pool whose single connection the unit holds (in-memory SQLite)."""

_HOLDER = "_pyfly_unit_holder"
"""The attribute of a single-connection pool that references (weakly) the unit holding its connection."""

_READ_ONLY_DIALECT_STATEMENT = {"mysql": "SET TRANSACTION READ ONLY", "mariadb": "SET TRANSACTION READ ONLY"}


_UNBOUND_FACTORY_HINT = (
    "the primary async_sessionmaker bean is bound to no AsyncEngine (binds={...} only), and a unit of work "
    "runs on one connection of one engine: @transactional, repositories, SessionProvider, infrastructure_unit() "
    "and the AsyncSession bean inside a unit run on DataSourceRegistry.primary (pyfly.data.relational.url) "
    "instead; bind the factory to its engine (async_sessionmaker(engine)) to make it the primary, and declare "
    "further databases under pyfly.data.relational.datasources.<name>"
)

_MANAGER = "_pyfly_transaction_manager"
"""The attribute a ``DataSource``, an ad-hoc session factory or an ad-hoc engine's ``sync_engine`` keeps its
manager in. The manager lives exactly as long as what it serves: a cache keyed by those objects would keep
them alive through the manager that references them."""

_LOOKUP_LOCK = threading.RLock()

_UNNAMED = object()
"""The registry an ad-hoc manager's name was worked out for, before it first was."""


class SqlAlchemyTransactionManager:
    """Runs units of work on one relational datasource (see the module documentation).

    *datasource* gives the units their engine, replica, begin options, capabilities and after-begin
    customizers; *sessionmaker* gives them their sessions (by default the datasource's own). Pass both for
    a session factory the application built over a registry engine: its session options (a session class,
    ``expire_on_commit``) with the datasource's treatment, and a read-only unit on the replica gets a
    session with those options bound to the replica's engine. A session factory alone is a datasource the
    registry does not know: the capabilities and begin options come from its engine's dialect, and there
    are no customizers. *name* is the datasource name units are bound under (by default the datasource's).
    """

    def __init__(
        self,
        datasource: DataSource | None = None,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        name: str | None = None,
    ) -> None:
        if datasource is None and sessionmaker is None:
            raise TypeError("SqlAlchemyTransactionManager needs a DataSource or an async_sessionmaker")
        if datasource is not None and sessionmaker is not None and sessionmaker.kw.get("bind") is not datasource.engine:
            raise ValueError(
                f"The session factory is not bound to the engine of datasource '{datasource.name}' "
                f"({datasource.masked_url}); pass the session factory alone"
            )
        self._datasource = datasource
        self._sessionmaker: async_sessionmaker[AsyncSession] = (
            sessionmaker if sessionmaker is not None else datasource.sessionmaker  # type: ignore[union-attr]
        )
        self._name = name or (datasource.name if datasource is not None else PRIMARY)
        # An ad-hoc manager named by for_sessionmaker() follows the installed registry (see datasource).
        self._follows_registry = datasource is None and name is None
        self._named_for: object = _UNNAMED
        self._capabilities: TransactionCapabilities | None = None

    # -- factories ------------------------------------------------------------------------------------------

    @classmethod
    def for_datasource(cls, datasource: DataSource) -> SqlAlchemyTransactionManager:
        """The manager of *datasource* (a replica maps to its primary's manager); one per datasource."""
        if datasource.is_replica and datasource.registry is not None:
            datasource = datasource.registry.get(datasource.name)
        return _attached(datasource, lambda: cls(datasource))

    @classmethod
    def for_sessionmaker(cls, factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyTransactionManager:
        """The manager of the datasource *factory* belongs to, or an ad-hoc manager for a factory the
        application built itself.

        An ad-hoc manager binds its units under the name of the installed registry's default datasource when
        both are on the same database (or no datasource is configured at all: the factory is the application's
        primary), and under a name of its own otherwise, so it never joins a unit on another database.
        """
        datasource = datasource_of(factory)
        if datasource is not None:
            return cls.for_datasource(datasource)
        return _attached(factory, lambda: cls(sessionmaker=factory))

    @classmethod
    def for_engine(cls, engine: AsyncEngine) -> SqlAlchemyTransactionManager:
        """The manager of the datasource built on *engine*, or an ad-hoc manager for a hand-built engine."""
        datasource = datasource_of(engine)
        if datasource is not None:
            return cls.for_datasource(datasource)
        # AsyncEngine has __slots__: the manager is kept on its sync engine.
        return _attached(
            engine.sync_engine, lambda: cls.for_sessionmaker(async_sessionmaker(engine, expire_on_commit=False))
        )

    # -- identity ---------------------------------------------------------------------------------------------

    @property
    def datasource(self) -> str:
        """The datasource name units are bound under.

        An ad-hoc manager's name follows the installed registry: it is worked out again when a context
        installs or removes its registry, so a factory used before the context started never keeps binding
        under the name of a datasource on another database.
        """
        if self._follows_registry:
            registry = installed_registry()
            if registry is not self._named_for:
                self._name = _adhoc_name(self._sessionmaker)
                self._named_for = registry
        return self._name

    @property
    def data_source(self) -> DataSource | None:
        """The registry datasource this manager serves (``None`` for an ad-hoc manager)."""
        return self._datasource

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory units get sessions configured like (the datasource's, unless another was given)."""
        return self._sessionmaker

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the datasource (not its replica)."""
        if self._datasource is not None:
            return self._datasource.engine
        bind = self._sessionmaker.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise IllegalTransactionStateError(
                f"The session factory of datasource '{self.datasource}' is not bound to an AsyncEngine"
            )
        return bind

    @property
    def dialect_capabilities(self) -> DataSourceCapabilities:
        """The dialect capabilities of the datasource (``DataSourceCapabilities``)."""
        if self._datasource is not None:
            return self._datasource.capabilities
        return DataSourceCapabilities.of(self.engine.dialect)

    @property
    def capabilities(self) -> TransactionCapabilities:
        """Savepoints, isolation levels, fast autocommit reads and multiple active results, from the dialect."""
        if self._capabilities is not None:
            return self._capabilities
        dialect = self.dialect_capabilities
        capabilities = TransactionCapabilities(
            backend=dialect.dialect,
            supports_savepoints=dialect.supports_savepoints,
            isolation_levels=dialect.isolation_levels,
            fast_autocommit_reads=dialect.fast_autocommit_reads,
            multiple_active_results=dialect.multiple_active_results,
        )
        if self.engine.dialect.server_version_info is not None:
            self._capabilities = capabilities  # final once the dialect has met the server
        return capabilities

    def owns(self, resource: object) -> bool:
        """Whether *resource* is this manager's session factory, or its datasource (or that datasource's
        engine) when the manager serves it under its own name."""
        if resource is self._sessionmaker:
            return True
        datasource = self._datasource
        return (
            datasource is not None
            and (resource is datasource.engine or resource is datasource)
            and self._name == datasource.name
        )

    # -- opening units --------------------------------------------------------------------------------------

    async def begin(self, definition: TransactionDefinition) -> UnitOfWork:
        """Open a transaction for *definition* (see the module documentation)."""
        read_only = definition.read_only
        target = self._datasource
        factory = self._sessionmaker
        bind: AsyncEngine | None = None
        if read_only and target is not None and target.replica is not None:
            if factory is target.sessionmaker:
                factory = target.replica.sessionmaker
            else:
                bind = target.replica.engine  # the application's session options, on the replica
            target = target.replica
        engine = target.engine if target is not None else self.engine
        dialect = _backend(engine)
        isolation = definition.isolation
        if not self.capabilities.supports_isolation(isolation):
            raise IllegalTransactionStateError(
                f"Isolation {isolation.value} is not supported on datasource '{self.datasource}' ({dialect}); "
                f"supported: {sorted(self.capabilities.isolation_levels)}",
                datasource=self.datasource,
            )
        if not read_only:
            self._refuse_waiting_for_own_lock(engine, definition.propagation.value)
        options = self._begin_options(target, dialect, read_only=read_only)
        if isolation.value != "DEFAULT":
            options["isolation_level"] = isolation.value
        if read_only and dialect == "postgresql":
            options["postgresql_readonly"] = True
        self._refuse_sharing_the_connection(engine)
        session = self._new_session(factory, bind=bind)
        unit = UnitOfWork(self, self.datasource, session, definition=definition)
        _claim_the_connection(engine, unit)
        await self._start(unit, session, options, target, read_only=read_only, dialect=dialect)
        if definition.timeout is not None and dialect == "postgresql":
            milliseconds = max(1, int(definition.timeout * 1000))
            await self._guard_start(
                unit, session, session.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))
            )
        return unit

    async def open_auto_unit(self, *, read_only: bool, autocommit: bool | None = None) -> UnitOfWork:
        """Open the short unit of a call outside a transaction (see the module documentation).

        *autocommit* ``None`` runs a read on an ``AUTOCOMMIT`` connection where the dialect makes that
        cheaper, ``True`` does so for a single writing statement too, and ``False`` never does.
        """
        engine = self.engine
        dialect = _backend(engine)
        target = self._datasource
        wanted = read_only if autocommit is None else autocommit
        autocommit = (
            wanted and self.capabilities.fast_autocommit_reads and not (target is not None and target.customizers)
        )
        if not read_only:
            self._refuse_waiting_for_own_lock(engine, "a write outside the suspended unit")
        options = (
            {"isolation_level": "AUTOCOMMIT"}
            if autocommit
            else self._begin_options(target, dialect, read_only=read_only)
        )
        self._refuse_sharing_the_connection(engine)
        # What an auto unit's call returns outlives its commit: it is never expired, whatever the factory says.
        session = self._new_session(self._sessionmaker, expire_on_commit=False)
        unit = UnitOfWork(self, self.datasource, session, auto=True, read_only=read_only)
        unit.attributes[_AUTOCOMMIT] = autocommit
        _claim_the_connection(engine, unit)
        await self._start(unit, session, options, None if autocommit else target, read_only=read_only, dialect=dialect)
        return unit

    def _new_session(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        bind: AsyncEngine | None = None,
        expire_on_commit: bool | None = None,
    ) -> UnitSession:
        session_class = unit_session_class(factory.class_)
        options = dict(factory.kw)
        if bind is not None:
            options["bind"] = bind
        if expire_on_commit is not None:
            options["expire_on_commit"] = expire_on_commit
        session = session_class(**options)
        assert isinstance(session, UnitSession)
        return session

    def _begin_options(self, target: DataSource | None, dialect: str, *, read_only: bool) -> dict[str, Any]:
        if target is not None:
            return dict(target.begin_options(read_only=read_only))
        return begin_execution_options(dialect, read_only=read_only)

    async def _start(
        self,
        unit: UnitOfWork,
        session: UnitSession,
        options: dict[str, Any],
        target: DataSource | None,
        *,
        read_only: bool,
        dialect: str,
    ) -> None:
        session._pyfly_unit = unit
        session.info["read_only"] = read_only
        if read_only:
            _install_read_only_guard(session, unit)

        try:
            # The checkout and BEGIN run under an anyio shield: SQLAlchemy cleans up a connection whose BEGIN
            # fails before the session holds it, and Starlette's level-triggered cancellation (a disconnected
            # stream) would cancel that cleanup too and leave the connection checked out forever. Shielded,
            # the BEGIN completes and the session holds the connection; a cancellation is honored right
            # after, and completing the unit discards that connection.
            with shield_scope():
                async with unit.operation():
                    connection = await AsyncSession.connection(session, execution_options=options)
                    if dialect == "sqlite":
                        # aiosqlite runs statements on a thread of its own: a discarded connection rolls
                        # back there before it closes, so it never keeps the write lock.
                        sqlite_discard.install(connection.engine)
                        unit.attributes[_DRIVER_CONNECTION] = _driver_of(connection)
                    statement = _READ_ONLY_DIALECT_STATEMENT.get(dialect) if read_only else None
                    if statement is not None and not unit.attributes.get(_AUTOCOMMIT):
                        await connection.exec_driver_sql(statement)
            if target is not None:
                await target.run_after_begin(session)
        except BaseException:
            await self._discard(unit)
            raise

    async def _guard_start(self, unit: UnitOfWork, session: UnitSession, statement: Any) -> None:
        try:
            await statement
        except BaseException:
            await self._discard(unit)
            raise

    async def _discard(self, unit: UnitOfWork) -> None:
        """Close the session of a unit that failed to start (shielded; its failure is the one to raise)."""
        _result, error, _cancelled = await run_shielded(self._close(unit, invalidate=unit.poisoned))
        if error is not None:
            _logger.debug("unit_of_work_discard_failed", exc_info=(type(error), error, error.__traceback__))

    def _refuse_sharing_the_connection(self, engine: AsyncEngine) -> None:
        pool = engine.sync_engine.pool
        if not isinstance(pool, (StaticPool, SingletonThreadPool)):
            return
        reference = getattr(pool, _HOLDER, None)
        holder = reference() if reference is not None else None
        if holder is not None:
            raise IllegalTransactionStateError(
                f"Datasource '{self.datasource}' is an in-memory SQLite database: it lives on one connection that "
                f"every session shares, and {holder.describe()} holds it, so another unit of work cannot start "
                "until that one ends (a concurrent call, REQUIRES_NEW, or a repository call while stream_all "
                "iterates). Use a file database (sqlite+aiosqlite:///path/to/app.db) for concurrent work.",
                datasource=self.datasource,
            )

    def _refuse_waiting_for_own_lock(self, engine: AsyncEngine, what: str) -> None:
        if engine.dialect.name != "sqlite" or not is_file_database(engine.url):
            return
        task = asyncio.current_task()
        for held in current_state().held_units(self.datasource):
            if held.owner_task is task and not held.read_only and not held.completed:
                raise IllegalTransactionStateError(
                    f"A new write unit on SQLite datasource '{self.datasource}' ({what}) would wait for the write "
                    f"lock that {held.describe()} holds, and that unit cannot finish before this one does. "
                    "SQLite has one writer: join the outer unit (Propagation.REQUIRED), run the work after it, "
                    "or put it on another datasource.",
                    datasource=self.datasource,
                )

    # -- completing units -----------------------------------------------------------------------------------

    async def commit(self, unit: UnitOfWork) -> None:
        """Flush and commit; a connection failure during ``COMMIT`` raises ``CommitOutcomeUnknownError``.

        A streamed result still open on a connection that has one active result at a time (MySQL, MariaDB)
        is closed first (``close_open_stream``): nothing else can run on that connection before it is."""
        session = unit.resource
        await close_open_stream(unit)
        await AsyncSession.flush(session)  # a failure here is definite: nothing was committed
        try:
            await AsyncSession.commit(session)
        except DBAPIError as error:
            if error.connection_invalidated:
                raise CommitOutcomeUnknownError(
                    f"The connection of {unit.describe()} failed while COMMIT was in flight; the unit may or may "
                    "not have committed. Do not retry it blindly.",
                    datasource=self.datasource,
                ) from error
            raise
        except (OSError, asyncio.CancelledError) as error:
            raise CommitOutcomeUnknownError(
                f"COMMIT of {unit.describe()} was interrupted in flight ({type(error).__name__}); the unit may or "
                "may not have committed. Do not retry it blindly.",
                datasource=self.datasource,
            ) from error

    async def rollback(self, unit: UnitOfWork) -> None:
        """Roll back, or discard the connection of a poisoned unit (its state is unknown).

        A read auto unit ends this way too; its entities are detached first, so what the call returned keeps
        its loaded state (a rollback would expire it). A streamed result still open on a connection that has
        one active result at a time is closed first, as for a commit.
        """
        session = unit.resource
        if unit.poisoned and _SHARED_CONNECTION not in unit.attributes:
            await _discard_connections(unit)
            return
        if unit.auto and unit.read_only:
            session.expunge_all()
        try:
            await close_open_stream(unit)
            await AsyncSession.rollback(session)
        except Exception:
            # The connection could not roll back (it was lost): make sure the pool discards it.
            await _discard_connections(unit)
            raise

    async def release(self, unit: UnitOfWork) -> None:
        """Close the session and return (or, when poisoned, discard) its connection. The single connection
        of an in-memory database is never discarded: that would drop the database."""
        await self._close(unit, invalidate=unit.poisoned and _SHARED_CONNECTION not in unit.attributes)

    async def _close(self, unit: UnitOfWork, *, invalidate: bool) -> None:
        try:
            if invalidate:
                await _discard_connections(unit)
            await AsyncSession.close(unit.resource)
        finally:
            _release_the_connection(unit)

    async def create_savepoint(self, unit: UnitOfWork) -> AsyncSessionTransaction:
        """``SAVEPOINT`` through ``session.begin_nested()``, recorded as the running task's (``track_savepoint``);
        refused while another task holds the unit's innermost savepoint."""
        async with unit.operation():
            savepoint = AsyncSession.begin_nested(unit.resource)
            await savepoint
            track_savepoint(unit, savepoint)
        # The template counts this savepoint once this returns: it is at the next depth.
        unit.attributes.setdefault(_TEMPLATE_SAVEPOINTS, []).append(
            (savepoint.sync_transaction, unit.savepoint_depth + 1)
        )
        return savepoint

    async def release_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """``RELEASE SAVEPOINT``, flushing what the nested scope left pending first.

        When that flush fails, SQLAlchemy rolls the savepoint back on the connection and leaves it open and
        deactivated: it stays the ``NESTED`` scope's until ``rollback_to_savepoint`` closes it. A streamed
        result the scope left open on a connection that has one active result at a time is closed first
        (``close_open_stream``): no savepoint opens while a stream is open, so it was opened inside the scope.
        """
        async with unit.operation(stream=unit.open_stream):
            await close_open_stream(unit)
            await savepoint.commit()
        _forget_template_savepoint(unit, savepoint)

    async def rollback_to_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """``ROLLBACK TO SAVEPOINT``; a streamed result the scope left open is closed first, as on release."""
        try:
            async with unit.operation(stream=unit.open_stream):
                await close_open_stream(unit)
                await savepoint.rollback()
        finally:
            _forget_template_savepoint(unit, savepoint)

    def failed_within_savepoint(self, unit: UnitOfWork, savepoint: Any) -> bool:
        """Whether a statement failed inside a savepoint the application opened within *savepoint* (a
        ``NESTED`` scope's) and left open: the ``NESTED`` scope then rolls back to *savepoint*."""
        container = savepoint.sync_transaction
        return any(_within(failed, container) for failed, _error in unit.attributes.get(_SAVEPOINT_FAILURES, ()))

    # -- state ---------------------------------------------------------------------------------------------

    def resource_active(self, unit: UnitOfWork) -> bool:
        """Whether the unit can still commit: its session's (outermost) transaction is active, and no
        savepoint the application opened and left open holds a failed statement.

        Asked as the unit completes; such a failure then marks the unit rollback-only (with the failure as
        the reason), as it would have if no savepoint had been open.
        """
        failures = unit.attributes.get(_SAVEPOINT_FAILURES)
        if failures:
            for _savepoint, error in failures:
                unit.set_rollback_only(error, depth=0)
            failures.clear()
            return False
        return _transaction_active(unit)

    def marks_rollback_only(self, unit: UnitOfWork, error: Exception) -> bool:
        """Every driver error marks the unit: on PostgreSQL the transaction is dead after any failed
        statement, and the same rule on every backend keeps one outcome for the same code.

        A statement that fails inside a savepoint the application opened (``session.begin_nested()``) is
        recorded against that savepoint instead: ``ROLLBACK TO SAVEPOINT`` leaves the transaction healthy on
        every backend, so the failure is forgotten when the savepoint rolls back (the ``async with
        session.begin_nested():`` idiom around an insert that may be a duplicate). It moves to the enclosing
        savepoint when the savepoint is released, and it marks the unit when the savepoint is still open as
        the unit completes (``resource_active``). The flush that releasing such a savepoint runs (the idiom
        with no flush inside the block) is the savepoint's too: when it fails, SQLAlchemy has rolled the
        savepoint back by the time the failure is raised, and nothing is left to mark.

        A failure does not poison the unit (its connection is healthy, and a rollback that fails discards it
        anyway): a driver error raised in place of a cancellation is handled by the operation guard, which
        poisons the unit and raises the cancellation instead.
        """
        marks = isinstance(error, DBAPIError) or not _transaction_active(unit)
        if marks and not unit.poisoned and not self.is_disconnect(error):
            if _rolled_back_on_release(unit):
                return False
            savepoint = _application_savepoint(unit)
            if savepoint is not None:
                _record_savepoint_failure(unit, savepoint, error)
                return False
        return marks

    def is_disconnect(self, error: BaseException) -> bool:
        """Whether *error* invalidated its connection (the server or the network dropped it)."""
        return isinstance(error, DBAPIError) and bool(error.connection_invalidated)

    def is_autocommit(self, unit: UnitOfWork) -> bool:
        """Whether *unit* runs on an ``AUTOCOMMIT`` connection (a fast read auto unit)."""
        return bool(unit.attributes.get(_AUTOCOMMIT))

    def __repr__(self) -> str:
        source = self._datasource.masked_url if self._datasource is not None else "ad-hoc session factory"
        return f"SqlAlchemyTransactionManager(datasource={self.datasource!r}, {source})"


# ---------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------


def _driver_of(connection: AsyncConnection) -> Any:
    """The driver connection under *connection* (no I/O: the connection is checked out already)."""
    sync_connection = connection.sync_connection
    return sync_connection.connection.driver_connection if sync_connection is not None else None


async def _discard_connections(unit: UnitOfWork) -> None:
    """Invalidate the unit's connections with the driver's forced terminate.

    The invalidation runs outside SQLAlchemy's greenlet on purpose: there the dialect terminates the
    connection at once (asyncpg aborts the socket, aiosqlite stops its thread) instead of attempting a
    graceful close, which can wait forever on a connection a cancelled statement left half-closed. The pool
    then drops the connection instead of handing it to the next caller.

    On SQLite the pool's discard listener has queued a rollback on aiosqlite's worker thread ahead of the
    close (interrupting a statement that still runs there); this waits, bounded, until it ran, so the next
    writer finds the write lock free.
    """
    try:
        unit.resource.sync_session.invalidate()
    except Exception:  # noqa: BLE001 — discarding is best effort; the connection is gone either way
        _logger.debug("unit_of_work_connection_discard_failed", exc_info=True)
    driver = unit.attributes.get(_DRIVER_CONNECTION)
    if driver is not None:
        await sqlite_discard.wait_released(driver)


def _claim_the_connection(engine: AsyncEngine, unit: UnitOfWork) -> None:
    """Record that *unit* holds the single connection of *engine*'s pool (in-memory SQLite)."""
    pool = engine.sync_engine.pool
    if isinstance(pool, (StaticPool, SingletonThreadPool)):
        setattr(pool, _HOLDER, weakref.ref(unit))
        unit.attributes[_SHARED_CONNECTION] = pool


def _release_the_connection(unit: UnitOfWork) -> None:
    pool = unit.attributes.get(_SHARED_CONNECTION)
    if pool is None:
        return
    reference = getattr(pool, _HOLDER, None)
    if reference is not None and reference() is unit:
        setattr(pool, _HOLDER, None)


def _transaction_active(unit: UnitOfWork) -> bool:
    """Whether the unit session's outermost transaction can still commit."""
    session: AsyncSession = unit.resource
    transaction = session.sync_session.get_transaction()
    return transaction is None or transaction.is_active


def _forget_template_savepoint(unit: UnitOfWork, savepoint: Any) -> None:
    held = unit.attributes.get(_TEMPLATE_SAVEPOINTS)
    if held:
        held[:] = [entry for entry in held if entry[0] is not savepoint.sync_transaction]


def _template_depth(unit: UnitOfWork, transaction: SessionTransaction) -> int | None:
    """The depth of *transaction* when it is a ``NESTED`` scope's savepoint, else ``None``."""
    for held, depth in unit.attributes.get(_TEMPLATE_SAVEPOINTS, ()):
        if held is transaction:
            return int(depth)
    return None


def _application_savepoint(unit: UnitOfWork) -> SessionTransaction | None:
    """The innermost savepoint of the unit's session when the application opened it (not a ``NESTED`` scope)."""
    session: AsyncSession = unit.resource
    nested = session.sync_session.get_nested_transaction()
    if nested is None or _template_depth(unit, nested) is not None:
        return None
    return nested


def _rolled_back_on_release(unit: UnitOfWork) -> bool:
    """Whether releasing an application savepoint failed and that savepoint is gone (SQLAlchemy rolled it back
    and closed it), leaving the transaction around it active."""
    marker = unit.attributes.get(RELEASING_SAVEPOINT)
    if marker is None:
        return False
    task, releasing = marker
    if task is not asyncio.current_task():
        return False  # another task's release: this failure is not its flush's
    sync_session = unit.resource.sync_session
    current = sync_session.get_nested_transaction() or sync_session.get_transaction()
    return current is not None and current.is_active and not _within(current, releasing)


def _within(transaction: SessionTransaction | None, container: SessionTransaction) -> bool:
    """Whether *transaction* is *container* or runs inside it."""
    current = transaction
    while current is not None:
        if current is container:
            return True
        current = current.parent
    return False


def _enclosing_savepoint(transaction: SessionTransaction) -> SessionTransaction | None:
    current = transaction.parent
    while current is not None and not current.nested:
        current = current.parent
    return current


def _record_savepoint_failure(unit: UnitOfWork, savepoint: SessionTransaction, error: BaseException) -> None:
    """Record *error* against the application's *savepoint* (the first failure per savepoint is kept)."""
    failures: list[tuple[SessionTransaction, BaseException]] | None = unit.attributes.get(_SAVEPOINT_FAILURES)
    if failures is None:
        failures = unit.attributes[_SAVEPOINT_FAILURES] = []
        sync_session = unit.resource.sync_session

        def _ended(_session: Session, transaction: SessionTransaction) -> None:
            # A savepoint that ends without being released rolled back (ROLLBACK TO SAVEPOINT, or the failed
            # flush's own rollback before the savepoint closed): what failed inside it is gone, and the
            # transaction is healthy again. A released one was handled by _released just before.
            failures[:] = [entry for entry in failures if not _within(entry[0], transaction)]

        def _released(session: Session) -> None:
            released = session.get_nested_transaction()
            if released is None:
                return
            for index, (failed, reason) in enumerate(failures):
                if failed is released:
                    del failures[index]
                    enclosing = _enclosing_savepoint(released)
                    depth = _template_depth(unit, enclosing) if enclosing is not None else 0
                    if enclosing is not None and depth is None:
                        _record_savepoint_failure(unit, enclosing, reason)  # the application's own savepoint
                    else:
                        unit.set_rollback_only(reason, depth=depth or 0)
                    return

        event.listen(sync_session, "after_commit", _released)
        event.listen(sync_session, "after_transaction_end", _ended)
    if not any(failed is savepoint for failed, _error in failures):
        failures.append((savepoint, error))


def _attached(owner: object, build: Callable[[], SqlAlchemyTransactionManager]) -> SqlAlchemyTransactionManager:
    """The manager kept on *owner* (see ``_MANAGER``), built and attached on first use."""
    manager = getattr(owner, _MANAGER, None)
    if isinstance(manager, SqlAlchemyTransactionManager):
        return manager
    with _LOOKUP_LOCK:
        manager = getattr(owner, _MANAGER, None)
        if not isinstance(manager, SqlAlchemyTransactionManager):
            manager = build()
            setattr(owner, _MANAGER, manager)
        return manager


def _backend(engine: AsyncEngine) -> str:
    dialect = engine.dialect
    return "mariadb" if getattr(dialect, "is_mariadb", False) else str(dialect.name)


def _install_read_only_guard(session: UnitSession, unit: UnitOfWork) -> None:
    """Refuse any ORM write in a read-only unit, whatever the backend."""

    def _refuse_writes(sync_session: Session, _flush_context: Any, _instances: Any) -> None:
        dirty = [instance for instance in sync_session.dirty if sync_session.is_modified(instance)]
        if not (sync_session.new or dirty or sync_session.deleted):
            return
        hint = (
            "A repository read method (find*, count*, exists*, stream*, get*) runs in a read-only auto unit; "
            "give a method that writes another name, or call it inside @transactional."
            if unit.auto
            else "Drop read_only=True from the boundary, or write in a unit of its own (Propagation.REQUIRES_NEW)."
        )
        raise IllegalTransactionStateError(
            f"{unit.describe()} is read-only and cannot flush {len(sync_session.new)} new, {len(dirty)} changed "
            f"and {len(sync_session.deleted)} deleted object(s). {hint}",
            datasource=unit.datasource,
        )

    event.listen(session.sync_session, "before_flush", _refuse_writes)


def _adhoc_name(factory: async_sessionmaker[AsyncSession]) -> str:
    registry = installed_registry()
    default = registry.default if registry is not None else None
    if not isinstance(default, SqlAlchemyTransactionManager):
        return PRIMARY
    bind = factory.kw.get("bind")
    if isinstance(bind, AsyncEngine) and _same_database(bind, default.engine):
        return default.datasource
    return f"session-factory-{id(factory):x}"


def _same_database(first: AsyncEngine, second: AsyncEngine) -> bool:
    one, other = make_url(first.url), make_url(second.url)
    return (one.get_backend_name(), one.host, one.port, one.database) == (
        other.get_backend_name(),
        other.host,
        other.port,
        other.database,
    )


def _resolve_resource(resource: object) -> SqlAlchemyTransactionManager | None:
    """The resource resolver the neutral registry asks (DataSource, session factory, engine, routing factory)."""
    if isinstance(resource, SqlAlchemyTransactionManager):
        return resource
    if isinstance(resource, DataSource):
        return SqlAlchemyTransactionManager.for_datasource(resource)
    if isinstance(resource, async_sessionmaker):
        return SqlAlchemyTransactionManager.for_sessionmaker(resource)
    if isinstance(resource, AsyncEngine):
        return SqlAlchemyTransactionManager.for_engine(resource)
    from pyfly.data.relational.routing import RoutingSessionFactory

    if isinstance(resource, RoutingSessionFactory) and isinstance(resource.primary_factory, async_sessionmaker):
        return SqlAlchemyTransactionManager.for_sessionmaker(resource.primary_factory)
    return None


register_resource_resolver(_resolve_resource)


_MANAGERS = "_pyfly_transaction_managers"
"""The attribute a ``DataSourceRegistry`` keeps its ``TransactionManagerRegistry`` in (see ``_MANAGER``)."""


def transaction_managers_for(datasources: DataSourceRegistry) -> TransactionManagerRegistry:
    """The :class:`~pyfly.data.transaction.registry.TransactionManagerRegistry` of *datasources*: its
    managers are the SQLAlchemy managers of the registry's datasources, built on first use (datasources a
    module registers later included), and its default datasource is the primary. One per
    ``DataSourceRegistry``, whichever auto-configuration asks first (the ``transaction_manager_registry``
    bean is that object); it lives as long as the ``DataSourceRegistry`` does. The primary's manager is the
    one :func:`bind_primary_session_factory` registered, when it was called."""
    existing = getattr(datasources, _MANAGERS, None)
    if isinstance(existing, TransactionManagerRegistry):
        return existing
    registry = TransactionManagerRegistry(default=PRIMARY)
    source = weakref.ref(datasources)

    def _resolve(name: str) -> SqlAlchemyTransactionManager | None:
        owner = source()
        if owner is None or owner.closed:
            return None
        try:
            datasource = owner.get(name)
        except (NoSuchDataSourceError, DataSourceConfigurationError):
            return None
        return SqlAlchemyTransactionManager.for_datasource(datasource)

    registry.add_resolver(_resolve)
    with _LOOKUP_LOCK:
        existing = getattr(datasources, _MANAGERS, None)
        if isinstance(existing, TransactionManagerRegistry):
            return existing
        setattr(datasources, _MANAGERS, registry)
        return registry


def bind_primary_session_factory(
    datasources: DataSourceRegistry, factory: async_sessionmaker[AsyncSession]
) -> SqlAlchemyTransactionManager:
    """Serve the ``primary`` datasource of :func:`transaction_managers_for` *datasources* on *factory*, the
    application's primary session factory, and return that manager.

    The relational auto-configuration calls it with the ``async_sessionmaker`` bean it resolved: the registry
    primary's own, or what replaced it (the application's singleton session factory bean, or the factory over
    its ``AsyncEngine`` bean). ``@transactional``, repository calls outside a transaction, ``SessionProvider``
    and ``infrastructure_unit()`` then run their ``primary`` units on that factory, and so does everything
    that maps the factory to its manager (``for_sessionmaker``: the ``AsyncSession`` bean,
    ``reactive_transactional``, a legacy ``_session_factory`` attribute). There is one primary, never one per
    path:

    - the registry primary's own factory: the primary's manager, as before;
    - a factory over an engine of *datasources* (the primary's, with other session options; a named
      datasource's): the factory's sessions with that datasource's replica, begin options and after-begin
      customizers, bound under ``primary``;
    - a factory over an engine the registry did not build: the factory's sessions, with the capabilities and
      begin options of its engine's dialect and no customizers. The registry never disposes that engine;
    - a factory bound to no engine (``binds={...}`` only): a unit of work runs on one connection of one
      engine, which such a factory does not name, so the registry primary's manager keeps serving the
      ``primary`` datasource, and a WARNING (``relational_session_factory_not_bound``) says so.

    Contexts started on one ``Config`` share its registry, and so its transaction managers: the last one
    bound serves them all. A context unbinds its own when it stops (:func:`unbind_primary_session_factory`).
    """
    owned = datasource_of(factory)
    if owned is not None and owned.registry is datasources and owned.name == PRIMARY and not owned.is_replica:
        manager = SqlAlchemyTransactionManager.for_datasource(owned)
    elif owned is None and not isinstance(factory.kw.get("bind"), AsyncEngine):
        _logger.warning("relational_session_factory_not_bound", extra={"hint": _UNBOUND_FACTORY_HINT})
        manager = SqlAlchemyTransactionManager.for_datasource(datasources.primary)
    else:
        bind = factory.kw.get("bind")
        owner = datasources.find_by_engine(bind) if isinstance(bind, AsyncEngine) else None
        with _LOOKUP_LOCK:
            attached = getattr(factory, _MANAGER, None)
            if (
                isinstance(attached, SqlAlchemyTransactionManager)
                and attached.data_source is owner
                and not attached._follows_registry
                and attached._name == PRIMARY
            ):
                manager = attached
            else:
                manager = SqlAlchemyTransactionManager(owner, sessionmaker=factory, name=PRIMARY)
                if owned is None:
                    # for_sessionmaker(factory) answers this manager from now on, not an ad-hoc one.
                    setattr(factory, _MANAGER, manager)
    transaction_managers_for(datasources).register(manager)
    return manager


def unbind_primary_session_factory(datasources: DataSourceRegistry, manager: SqlAlchemyTransactionManager) -> None:
    """Undo :func:`bind_primary_session_factory` for *manager*: it stops serving the ``primary`` datasource
    of :func:`transaction_managers_for` *datasources*, and its session factory stops mapping to it, unless
    another binding replaced it since (a context started later on the same configuration). The context that
    bound it calls this when it stops; the registry's own primary serves ``primary`` again."""
    managers = getattr(datasources, _MANAGERS, None)
    if isinstance(managers, TransactionManagerRegistry):
        managers.unregister(manager)
    factory = manager.sessionmaker
    with _LOOKUP_LOCK:
        if getattr(factory, _MANAGER, None) is manager:
            delattr(factory, _MANAGER)
