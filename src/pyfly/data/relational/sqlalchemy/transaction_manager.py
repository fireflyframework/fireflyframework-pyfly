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
that no registry owns gets an ad-hoc manager of its own (``for_sessionmaker``). Every unit it opens gets a
fresh :class:`~pyfly.data.relational.sqlalchemy.session.UnitSession` and, before its first statement:

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

Auto units (a repository call outside a transaction) use the same path. A read auto unit on a dialect with
fast autocommit reads (PostgreSQL) runs on an ``AUTOCOMMIT`` connection, one round trip instead of three,
unless the datasource has after-begin customizers: their transaction-local settings would not outlive
their own statement there. A write auto unit commits.

A commit whose connection fails while ``COMMIT`` is in flight raises
:class:`~pyfly.data.transaction.errors.CommitOutcomeUnknownError`. A unit whose operation was cancelled in
flight (the unit is *poisoned*) has its connection invalidated instead of rolled back, so the pool never
gets back a connection in an unknown state.

SQLite has one writer. A new write unit that would wait for the write lock of a unit the same task keeps
open (it suspended it with ``REQUIRES_NEW`` or ``NOT_SUPPORTED``) fails at once with
:class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` instead of waiting ``busy_timeout``
for itself.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from sqlalchemy.orm import Session

from pyfly.data.relational.datasource_registry import (
    DataSource,
    DataSourceCapabilities,
    DataSourceConfigurationError,
    DataSourceRegistry,
    NoSuchDataSourceError,
    datasource_of,
)
from pyfly.data.relational.dialect_customizers import begin_execution_options, is_file_database
from pyfly.data.relational.sqlalchemy.session import UnitSession, unit_session_class
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import CommitOutcomeUnknownError, IllegalTransactionStateError, TransactionError
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.registry import (
    PRIMARY,
    TransactionManagerRegistry,
    installed_registry,
    register_resource_resolver,
)
from pyfly.data.transaction.template import run_shielded, shield_scope
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = ["SqlAlchemyTransactionManager", "transaction_managers_for"]

_logger = logging.getLogger(__name__)

_AUTOCOMMIT = "pyfly_autocommit"
"""``UnitOfWork.attributes`` key: the unit runs on an ``AUTOCOMMIT`` connection."""

_DRIVER_CONNECTION = "pyfly_driver_connection"
"""``UnitOfWork.attributes`` key: the driver connection of a SQLite unit, to interrupt a statement in flight."""

_READ_ONLY_DIALECT_STATEMENT = {"mysql": "SET TRANSACTION READ ONLY", "mariadb": "SET TRANSACTION READ ONLY"}


class SqlAlchemyTransactionManager:
    """Runs units of work on one relational datasource (see the module documentation)."""

    _by_datasource: weakref.WeakKeyDictionary[DataSource, SqlAlchemyTransactionManager] = weakref.WeakKeyDictionary()
    _by_sessionmaker: weakref.WeakKeyDictionary[async_sessionmaker[AsyncSession], SqlAlchemyTransactionManager] = (
        weakref.WeakKeyDictionary()
    )
    _by_engine: weakref.WeakKeyDictionary[AsyncEngine, SqlAlchemyTransactionManager] = weakref.WeakKeyDictionary()

    def __init__(
        self,
        datasource: DataSource | None = None,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        name: str | None = None,
    ) -> None:
        if datasource is None and sessionmaker is None:
            raise TypeError("SqlAlchemyTransactionManager needs a DataSource or an async_sessionmaker")
        self._datasource = datasource
        self._sessionmaker: async_sessionmaker[AsyncSession] = (
            datasource.sessionmaker if datasource is not None else sessionmaker  # type: ignore[assignment]
        )
        self._name = name or (datasource.name if datasource is not None else PRIMARY)
        self._capabilities: TransactionCapabilities | None = None

    # -- factories ------------------------------------------------------------------------------------------

    @classmethod
    def for_datasource(cls, datasource: DataSource) -> SqlAlchemyTransactionManager:
        """The manager of *datasource* (a replica maps to its primary's manager); one per datasource."""
        if datasource.is_replica and datasource.registry is not None:
            datasource = datasource.registry.get(datasource.name)
        manager = cls._by_datasource.get(datasource)
        if manager is None:
            manager = cls(datasource)
            cls._by_datasource[datasource] = manager
        return manager

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
        manager = cls._by_sessionmaker.get(factory)
        if manager is None:
            manager = cls(sessionmaker=factory, name=_adhoc_name(factory))
            cls._by_sessionmaker[factory] = manager
        return manager

    @classmethod
    def for_engine(cls, engine: AsyncEngine) -> SqlAlchemyTransactionManager:
        """The manager of the datasource built on *engine*, or an ad-hoc manager for a hand-built engine."""
        datasource = datasource_of(engine)
        if datasource is not None:
            return cls.for_datasource(datasource)
        manager = cls._by_engine.get(engine)
        if manager is None:
            manager = cls.for_sessionmaker(async_sessionmaker(engine, expire_on_commit=False))
            cls._by_engine[engine] = manager
        return manager

    # -- identity ---------------------------------------------------------------------------------------------

    @property
    def datasource(self) -> str:
        """The datasource name units are bound under."""
        return self._name

    @property
    def data_source(self) -> DataSource | None:
        """The registry datasource this manager serves (``None`` for an ad-hoc manager)."""
        return self._datasource

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory of the datasource (units get sessions configured like its sessions)."""
        return self._sessionmaker

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the datasource (not its replica)."""
        if self._datasource is not None:
            return self._datasource.engine
        bind = self._sessionmaker.kw.get("bind")
        if not isinstance(bind, AsyncEngine):
            raise IllegalTransactionStateError(
                f"The session factory of datasource '{self._name}' is not bound to an AsyncEngine"
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
        """Savepoints, isolation levels and fast autocommit reads, from the dialect."""
        if self._capabilities is not None:
            return self._capabilities
        dialect = self.dialect_capabilities
        capabilities = TransactionCapabilities(
            backend=dialect.dialect,
            supports_savepoints=dialect.supports_savepoints,
            isolation_levels=dialect.isolation_levels,
            fast_autocommit_reads=dialect.fast_autocommit_reads,
        )
        if self.engine.dialect.server_version_info is not None:
            self._capabilities = capabilities  # final once the dialect has met the server
        return capabilities

    def owns(self, resource: object) -> bool:
        """Whether *resource* is this datasource's session factory or engine."""
        if resource is self._sessionmaker:
            return True
        return self._datasource is not None and resource is self._datasource.engine

    # -- opening units --------------------------------------------------------------------------------------

    async def begin(self, definition: TransactionDefinition) -> UnitOfWork:
        """Open a transaction for *definition* (see the module documentation)."""
        read_only = definition.read_only
        target = self._datasource
        if read_only and target is not None and target.replica is not None:
            target = target.replica
        factory = target.sessionmaker if target is not None else self._sessionmaker
        engine = target.engine if target is not None else self.engine
        dialect = _backend(engine)
        isolation = definition.isolation
        if not self.capabilities.supports_isolation(isolation):
            raise IllegalTransactionStateError(
                f"Isolation {isolation.value} is not supported on datasource '{self._name}' ({dialect}); "
                f"supported: {sorted(self.capabilities.isolation_levels)}",
                datasource=self._name,
            )
        if not read_only:
            self._refuse_waiting_for_own_lock(engine, definition.propagation.value)
        options = self._begin_options(target, dialect, read_only=read_only)
        if isolation.value != "DEFAULT":
            options["isolation_level"] = isolation.value
        if read_only and dialect == "postgresql":
            options["postgresql_readonly"] = True
        session = self._new_session(factory)
        unit = UnitOfWork(self, self._name, session, definition=definition)
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
        session = self._new_session(self._sessionmaker)
        unit = UnitOfWork(self, self._name, session, auto=True, read_only=read_only)
        unit.attributes[_AUTOCOMMIT] = autocommit
        await self._start(unit, session, options, None if autocommit else target, read_only=read_only, dialect=dialect)
        return unit

    def _new_session(self, factory: async_sessionmaker[AsyncSession]) -> UnitSession:
        session_class = unit_session_class(factory.class_)
        session = session_class(**factory.kw)
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
                        # aiosqlite runs a statement on its own thread; a cancelled one is interrupted there.
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

    def _refuse_waiting_for_own_lock(self, engine: AsyncEngine, what: str) -> None:
        if engine.dialect.name != "sqlite" or not is_file_database(engine.url):
            return
        task = asyncio.current_task()
        for held in current_state().held_units(self._name):
            if held.owner_task is task and not held.read_only and not held.completed:
                raise IllegalTransactionStateError(
                    f"A new write unit on SQLite datasource '{self._name}' ({what}) would wait for the write "
                    f"lock that {held.describe()} holds, and that unit cannot finish before this one does. "
                    "SQLite has one writer: join the outer unit (Propagation.REQUIRED), run the work after it, "
                    "or put it on another datasource.",
                    datasource=self._name,
                )

    # -- completing units -----------------------------------------------------------------------------------

    async def commit(self, unit: UnitOfWork) -> None:
        """Flush and commit; a connection failure during ``COMMIT`` raises ``CommitOutcomeUnknownError``."""
        session = unit.resource
        await AsyncSession.flush(session)  # a failure here is definite: nothing was committed
        try:
            await AsyncSession.commit(session)
        except DBAPIError as error:
            if error.connection_invalidated:
                raise CommitOutcomeUnknownError(
                    f"The connection of {unit.describe()} failed while COMMIT was in flight; the unit may or may "
                    "not have committed. Do not retry it blindly.",
                    datasource=self._name,
                ) from error
            raise
        except (OSError, asyncio.CancelledError) as error:
            raise CommitOutcomeUnknownError(
                f"COMMIT of {unit.describe()} was interrupted in flight ({type(error).__name__}); the unit may or "
                "may not have committed. Do not retry it blindly.",
                datasource=self._name,
            ) from error

    async def rollback(self, unit: UnitOfWork) -> None:
        """Roll back, or discard the connection of a poisoned unit (its state is unknown).

        A read auto unit ends this way too; its entities are detached first, so what the call returned keeps
        its loaded state (a rollback would expire it).
        """
        session = unit.resource
        if unit.poisoned:
            await _discard_connections(unit)
            return
        if unit.auto and unit.read_only:
            session.expunge_all()
        try:
            await AsyncSession.rollback(session)
        except Exception:
            # The connection could not roll back (it was lost): make sure the pool discards it.
            await _discard_connections(unit)
            raise

    async def release(self, unit: UnitOfWork) -> None:
        """Close the session and return (or, when poisoned, discard) its connection."""
        await self._close(unit, invalidate=unit.poisoned)

    async def _close(self, unit: UnitOfWork, *, invalidate: bool) -> None:
        if invalidate:
            await _discard_connections(unit)
        await AsyncSession.close(unit.resource)

    async def create_savepoint(self, unit: UnitOfWork) -> AsyncSessionTransaction:
        """``SAVEPOINT`` through ``session.begin_nested()``."""
        async with unit.operation():
            savepoint = AsyncSession.begin_nested(unit.resource)
            await savepoint
            return savepoint

    async def release_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """``RELEASE SAVEPOINT`` (flushing what the nested scope left pending)."""
        async with unit.operation():
            await savepoint.commit()

    async def rollback_to_savepoint(self, unit: UnitOfWork, savepoint: Any) -> None:
        """``ROLLBACK TO SAVEPOINT``."""
        async with unit.operation():
            await savepoint.rollback()

    # -- state ---------------------------------------------------------------------------------------------

    def resource_active(self, unit: UnitOfWork) -> bool:
        """Whether the session's (outermost) transaction can still commit."""
        session: AsyncSession = unit.resource
        transaction = session.sync_session.get_transaction()
        return transaction is None or transaction.is_active

    def marks_rollback_only(self, unit: UnitOfWork, error: Exception) -> bool:
        """Every driver error marks the unit: on PostgreSQL the transaction is dead after any failed
        statement, and the same rule on every backend keeps one outcome for the same code.

        An error that is neither SQLAlchemy's nor the unit of work's also poisons the unit: a raw driver
        error (aiosqlite's "Connection closed" once its thread stopped), or what SQLAlchemy raised in place
        of a cancellation that hit its own cleanup, leaves the connection in an unknown state, so it is
        discarded instead of being awaited again.
        """
        if not isinstance(error, (SQLAlchemyError, TransactionError)):
            unit.poisoned = True
        return isinstance(error, DBAPIError) or not self.resource_active(unit)

    def is_disconnect(self, error: BaseException) -> bool:
        """Whether *error* invalidated its connection (the server or the network dropped it)."""
        return isinstance(error, DBAPIError) and bool(error.connection_invalidated)

    def is_autocommit(self, unit: UnitOfWork) -> bool:
        """Whether *unit* runs on an ``AUTOCOMMIT`` connection (a fast read auto unit)."""
        return bool(unit.attributes.get(_AUTOCOMMIT))

    def __repr__(self) -> str:
        source = self._datasource.masked_url if self._datasource is not None else "ad-hoc session factory"
        return f"SqlAlchemyTransactionManager(datasource={self._name!r}, {source})"


# ---------------------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------------------


def _driver_of(connection: AsyncConnection) -> Any:
    """The driver connection under *connection* (no I/O: the connection is checked out already)."""
    sync_connection = connection.sync_connection
    return sync_connection.connection.driver_connection if sync_connection is not None else None


async def _discard_connections(unit: UnitOfWork) -> None:
    """Invalidate the unit's connections with the driver's forced terminate, without waiting.

    A SQLite statement still running on aiosqlite's thread is interrupted first (a cancelled write would
    otherwise keep the database's write lock until it finished). The invalidation runs outside SQLAlchemy's
    greenlet on purpose: there the dialect terminates the connection at once (asyncpg aborts the socket,
    aiosqlite stops its thread) instead of attempting a graceful close, which can wait forever on a
    connection a cancelled statement left half-closed. The pool then drops the connection instead of handing
    it to the next caller.
    """
    driver = unit.attributes.get(_DRIVER_CONNECTION)
    interrupt = getattr(driver, "interrupt", None)
    if callable(interrupt):
        try:
            await interrupt()
        except Exception:  # noqa: BLE001 — nothing may be running, or the connection may be closed already
            _logger.debug("unit_of_work_statement_interrupt_failed", exc_info=True)
    try:
        unit.resource.sync_session.invalidate()
    except Exception:  # noqa: BLE001 — discarding is best effort; the connection is gone either way
        _logger.debug("unit_of_work_connection_discard_failed", exc_info=True)


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


_REGISTRIES: weakref.WeakKeyDictionary[DataSourceRegistry, TransactionManagerRegistry] = weakref.WeakKeyDictionary()


def transaction_managers_for(datasources: DataSourceRegistry) -> TransactionManagerRegistry:
    """The :class:`~pyfly.data.transaction.registry.TransactionManagerRegistry` of *datasources*: its
    managers are the SQLAlchemy managers of the registry's datasources, built on first use (datasources a
    module registers later included), and its default datasource is the primary. One per
    ``DataSourceRegistry``, whichever auto-configuration asks first (the ``transaction_manager_registry``
    bean is that object)."""
    existing = _REGISTRIES.get(datasources)
    if existing is not None:
        return existing
    registry = TransactionManagerRegistry(default=PRIMARY)

    def _resolve(name: str) -> SqlAlchemyTransactionManager | None:
        if datasources.closed:
            return None
        try:
            datasource = datasources.get(name)
        except (NoSuchDataSourceError, DataSourceConfigurationError):
            return None
        return SqlAlchemyTransactionManager.for_datasource(datasource)

    registry.add_resolver(_resolve)
    return _REGISTRIES.setdefault(datasources, registry)
