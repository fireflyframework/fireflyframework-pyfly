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
"""The transaction manager of a test's rollback transaction (:class:`pyfly.testing.RollbackTransaction`).

While a rollback transaction runs, a :class:`RollbackTransactionManager` replaces a relational datasource's
manager in the context's ``TransactionManagerRegistry``: every unit of work of the test becomes a savepoint of
the one transaction the test holds on its connection, which rolls back when the test ends.

The test's units share that one connection, so no cancellation may interrupt a statement on it: SQLAlchemy takes
a ``CancelledError`` that lands while a statement runs for a disconnect and invalidates the connection, which
would end the test's transaction. Each database round trip of a unit (a statement through its session, its
``SAVEPOINT``, the savepoints of ``NESTED`` scopes and of ``session.begin_nested()``) therefore runs to its end
in a task of its own (:func:`_round_trip`), and a cancellation that arrives meanwhile is raised when it returns.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from typing import Any, TypeVar

from sqlalchemy import event
from sqlalchemy.engine import Connection, Result
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    AsyncSessionTransaction,
    async_sessionmaker,
)
from sqlalchemy.orm import ORMExecuteState

from pyfly.data.relational.datasource_registry import DataSource
from pyfly.data.relational.sqlalchemy import transaction_manager as _adapter
from pyfly.data.relational.sqlalchemy.session import UnitSavepoint, UnitSession, close_open_stream, track_savepoint
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.template import shield_scope
from pyfly.data.transaction.unit_of_work import UnitOfWork, UnitStatus

try:
    import anyio.lowlevel
except ImportError:  # pragma: no cover — anyio ships with every web stack PyFly supports
    _CHECKPOINT_IF_CANCELLED: Callable[[], Awaitable[None]] | None = None
else:
    _CHECKPOINT_IF_CANCELLED = anyio.lowlevel.checkpoint_if_cancelled

T = TypeVar("T")

# The dialect statements a unit issues right after BEGIN (MySQL's SET TRANSACTION READ ONLY) set the
# characteristics of a transaction that has not started yet: inside the test's transaction they fail.
_NO_BEGIN_STATEMENTS = {"mysql": "mysql (savepoint)", "mariadb": "mariadb (savepoint)"}

_logger = logging.getLogger(__name__)

# A unit in one of these states has released (or rolled back to) its savepoint: it no longer holds the
# connection, even before its session closes.
_ENDED = (UnitStatus.COMMITTED, UnitStatus.ROLLED_BACK)

# The units whose statements ran inside a unit's savepoint while it was the innermost one, from a task that
# neither owns it nor waits for it (the unit's attribute).
_FOREIGN_UNITS = "pyfly_rollback_foreign_units"

# The statements that start and end the units' savepoints: they belong to the units' own machinery.
_SAVEPOINT_STATEMENTS = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")

_ISSUING_TASK: ContextVar[asyncio.Task[Any] | None] = ContextVar("pyfly_rollback_issuing_task", default=None)
"""The task a round trip runs for (:func:`_round_trip`), in the context of the task that runs it."""


def _issuing_task() -> asyncio.Task[Any] | None:
    """The task a statement on the test's connection runs for: the task that started the round trip it runs in
    (:func:`_round_trip`), or else the running task."""
    return _ISSUING_TASK.get() or asyncio.current_task()


async def _round_trip(operation: Coroutine[Any, Any, T]) -> T:
    """Run *operation*, a database round trip of a test's unit, to its end whatever happens to the calling task;
    then raise the cancellation that arrived meanwhile, if one did, or return its outcome.

    It runs in a task of its own, which a cancellation of the calling task does not reach (a native one:
    ``Task.cancel()``, ``asyncio.timeout``, ``asyncio.wait_for``, a unit's ``timeout=``), awaited under an anyio
    shield (an anyio cancel scope delivers its cancellation once the shield is gone). The cancellation is raised
    inside the unit's operation guard, which the calling task holds: the guard poisons the unit, and the unit
    rolls back to its savepoint on the healthy connection, as it rolls back in production, where its connection
    is discarded instead. The statement is not cut short: a cancelled call returns once it ends (on PostgreSQL
    a unit's ``timeout=`` still cancels it on the server, through ``statement_timeout``). The listeners of the
    test's connection see the calling task as the running one (:func:`_issuing_task`).
    """
    context = contextvars.copy_context()
    context.run(_ISSUING_TASK.set, _issuing_task())
    task = asyncio.get_running_loop().create_task(operation, context=context)
    cancelled: asyncio.CancelledError | None = None
    with shield_scope():
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as cancellation:
                cancelled = cancelled or cancellation
    failure = None if task.cancelled() else task.exception()
    if cancelled is None and _CHECKPOINT_IF_CANCELLED is not None:
        try:
            await _CHECKPOINT_IF_CANCELLED()  # an anyio cancel scope that expired meanwhile delivers it here
        except asyncio.CancelledError as cancellation:
            cancelled = cancellation
    if cancelled is not None:
        if failure is not None:
            cancelled.__cause__ = failure  # the statement's own failure, as the cancellation's cause
        raise cancelled
    return task.result()


class _ShieldedRoundTrips(AsyncSession):
    """The ``AsyncSession`` operations that reach the database, each run as a round trip of its own
    (:func:`_round_trip`). It sits under :class:`UnitSession` in a test unit's session class, so the unit's
    operation guard stays with the calling task: only the database work runs apart."""

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().execute(*args, **kwargs))

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().scalar(*args, **kwargs))

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().scalars(*args, **kwargs))

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().get(*args, **kwargs))

    async def get_one(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().get_one(*args, **kwargs))

    async def merge(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().merge(*args, **kwargs))

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().delete(*args, **kwargs))

    async def flush(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().flush(*args, **kwargs))

    async def refresh(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().refresh(*args, **kwargs))

    async def run_sync(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().run_sync(*args, **kwargs))

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().connection(*args, **kwargs))

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        return await _round_trip(super().stream(*args, **kwargs))


class _ShieldedTransaction(AsyncSessionTransaction):
    """A savepoint whose ``SAVEPOINT``, ``RELEASE SAVEPOINT`` (with the flush it runs) and ``ROLLBACK TO
    SAVEPOINT`` are round trips of their own (:func:`_round_trip`): a ``NESTED`` scope's."""

    __slots__ = ()

    async def start(self, is_ctxmanager: bool = False) -> AsyncSessionTransaction:
        return await _round_trip(super().start(is_ctxmanager))

    async def commit(self) -> None:
        await _round_trip(super().commit())

    async def rollback(self) -> None:
        await _round_trip(super().rollback())

    async def __aexit__(self, type_: Any, value: Any, traceback: Any) -> None:
        await _round_trip(super().__aexit__(type_, value, traceback))


class _RollbackSavepoint(UnitSavepoint, _ShieldedTransaction):
    """A savepoint the application opens on a test unit's session (``session.begin_nested()``): a
    :class:`UnitSavepoint` whose round trips run apart, under the guard the calling task holds."""

    __slots__ = ()


class _RollbackUnitSession(UnitSession, _ShieldedRoundTrips):
    """The session of a unit of a test that rolls back: a :class:`UnitSession` whose database round trips run to
    their end whatever happens to the calling task (:func:`_round_trip`)."""

    async def stream_scalars(self, *args: Any, **kwargs: Any) -> Any:
        # UnitSession.stream_scalars() opens its stream through AsyncSession.stream() itself, past the round trip:
        # open it through stream() (guarded, apart) and take its scalars, as AsyncSession.stream_scalars() does.
        result = await self.stream(*args, **kwargs)
        return result.scalars()

    def begin_nested(self) -> AsyncSessionTransaction:
        unit = self._pyfly_unit
        if unit is None:
            return super().begin_nested()
        return _RollbackSavepoint(self, unit)


_ROLLBACK_SESSION_CLASSES: dict[type[AsyncSession], type[AsyncSession]] = {AsyncSession: _RollbackUnitSession}


def _rollback_session_class(session_class: type[AsyncSession]) -> type[AsyncSession]:
    """The class of a test unit's session, for a session factory's class (a custom ``AsyncSession`` subclass keeps
    its behavior, and its round trips run apart too)."""
    found = _ROLLBACK_SESSION_CLASSES.get(session_class)
    if found is None:
        if issubclass(session_class, _RollbackUnitSession):
            found = session_class
        else:
            found = type(f"Rollback{session_class.__name__}", (_RollbackUnitSession, session_class), {})
        _ROLLBACK_SESSION_CLASSES[session_class] = found
    return found


class RollbackTransactionManager(SqlAlchemyTransactionManager):
    """Serves one datasource's units of work, while a rollback transaction runs, as savepoints of the test's
    transaction on *connection*.

    Every unit gets a session bound to *connection* with ``join_transaction_mode="create_savepoint"``: its
    ``BEGIN`` is a ``SAVEPOINT``, its commit a ``RELEASE SAVEPOINT`` and its rollback a ``ROLLBACK TO
    SAVEPOINT``, so each unit still completes on its own. The settings that belong to a transaction (the
    isolation level, ``BEGIN IMMEDIATE``, a read-only transaction, an autocommit connection) are the test
    transaction's: a unit's own are not applied; a read-only unit keeps refusing ORM writes.

    A unit's round trips (its statements through its session, its ``SAVEPOINT``, the statements of the
    after-begin customizers, the savepoints of ``NESTED`` scopes and of ``session.begin_nested()``) run to
    their end when its task is cancelled (a cancel scope, ``asyncio.wait_for``, the unit's own ``timeout=``):
    the cancellation is raised once the round trip returns (see :func:`_round_trip`), the unit rolls back to its
    savepoint, and the test's transaction goes on. The connection is never discarded for a unit, which would end
    that transaction. A statement that runs past the unit's session (one on the connection ``session.connection()``
    returns, a lazy load through ``awaitable_attrs``) is not a round trip of the unit: a cancellation can still
    cut it short and lose the connection, and every later unit then fails at once with
    ``IllegalTransactionStateError``, which names the unit the connection was lost in.

    The units share one connection, so they must nest: a unit that starts while a unit of another task is
    open on it (``asyncio.gather`` of ``@transactional`` calls or of repository calls, a background task
    started during the test) is refused with ``IllegalTransactionStateError`` before it touches the
    connection. Their savepoints would interleave, the first release would fail with "no such savepoint",
    and the test's transaction would be lost for the rest of the test. A task that waits for the units of
    tasks it started (they see its unit) is not refused. Such a task must wait, though: a statement it runs
    through its own unit while the unit of a task it started is the innermost one on the connection runs inside
    that unit's savepoint. That unit's rollback would undo it, with no error; the statement's unit is marked
    rollback-only instead (its commit fails with ``UnexpectedRollbackError``, caused by an
    ``IllegalTransactionStateError`` that says why).

    A read auto unit that ends healthy releases its savepoint (see :meth:`rollback`), and a streamed statement
    reads its rows when it runs (see :func:`_read_streams_in_full`).

    A unit that *serves* asks (a callable) whether the running task belongs to the test; units of other tasks
    (the context's background work started before the test) run on *original*, the datasource's own manager.
    """

    def __init__(
        self,
        original: SqlAlchemyTransactionManager,
        connection: AsyncConnection,
        serves: Callable[[], bool],
    ) -> None:
        datasource: DataSource | None = original.data_source
        factory: async_sessionmaker[AsyncSession] = original.sessionmaker
        super().__init__(datasource, sessionmaker=factory, name=original.datasource)
        self._original = original
        self._connection = connection
        self._serves = serves
        self._open: list[UnitOfWork] = []  # the units on the connection, in the order they started
        self._lost_in: str | None = None  # the unit the connection was lost in (see _refuse_a_lost_connection)
        sync_connection = connection.sync_connection
        if sync_connection is not None:
            event.listen(sync_connection, "before_cursor_execute", self._note_foreign_statement)

    @property
    def original(self) -> SqlAlchemyTransactionManager:
        """The datasource's own manager, which serves the datasource again when the test ends."""
        return self._original

    @property
    def connection(self) -> AsyncConnection:
        """The test's connection: every unit of the test runs on it."""
        return self._connection

    @property
    def engine(self) -> AsyncEngine:
        return self._original.engine

    @property
    def capabilities(self) -> TransactionCapabilities:
        # Every unit is a savepoint of the test's transaction: none runs on an autocommit connection.
        return dataclasses.replace(self._original.capabilities, fast_autocommit_reads=False)

    async def begin(self, definition: TransactionDefinition) -> UnitOfWork:
        if not self._serves():
            return await self._original.begin(definition)
        self._refuse_a_lost_connection()
        return await super().begin(definition)

    async def open_auto_unit(self, *, read_only: bool, autocommit: bool | None = None) -> UnitOfWork:
        if not self._serves():
            return await self._original.open_auto_unit(read_only=read_only, autocommit=autocommit)
        self._refuse_a_lost_connection()
        return await super().open_auto_unit(read_only=read_only, autocommit=False)

    def _connection_lost(self) -> bool:
        sync_connection = self._connection.sync_connection
        return sync_connection is not None and sync_connection.invalidated

    def _refuse_a_lost_connection(self) -> None:
        """Refuse a new unit once the test's connection is lost: the test's transaction went with it."""
        if not self._connection_lost():
            return
        where = f" while {self._lost_in} was open" if self._lost_in is not None else ""
        raise IllegalTransactionStateError(
            f"The test's connection to datasource '{self.datasource}' was lost{where}, and the test's transaction "
            "with it: what the test wrote is gone, and no unit of work can run on that connection until the test "
            "ends. A unit's statements through its session run to their end when its task is cancelled; a "
            "statement that ran past the session was cut short (one on the connection session.connection() "
            "returns, or a lazy load through awaitable_attrs, cancelled while it ran), or the database closed the "
            "connection.",
            datasource=self.datasource,
        )

    def _new_session(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        bind: AsyncEngine | None = None,
        expire_on_commit: bool | None = None,
    ) -> UnitSession:
        options = dict(factory.kw)
        options.pop("binds", None)
        options["bind"] = self._connection
        options["join_transaction_mode"] = "create_savepoint"
        if expire_on_commit is not None:
            options["expire_on_commit"] = expire_on_commit
        session = _rollback_session_class(factory.class_)(**options)
        assert isinstance(session, UnitSession)
        event.listen(session.sync_session, "do_orm_execute", _read_streams_in_full)
        return session

    def _begin_options(self, target: DataSource | None, dialect: str, *, read_only: bool) -> dict[str, Any]:
        return {}

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
        # Checked and recorded before the first await: two tasks starting at once see each other.
        self._refuse_overlapping(unit)
        self._open.append(unit)
        # A cancelled unit rolls back to its savepoint: discarding the connection would end the test's
        # transaction (the adapter keeps an in-memory database's single connection the same way).
        unit.attributes.setdefault(_adapter._SHARED_CONNECTION, self.engine.sync_engine.pool)
        try:
            # The unit's SAVEPOINT, as a round trip of its own: the datasource's manager sends its BEGIN under an
            # anyio shield only, which a native cancellation (asyncio.timeout, wait_for) goes through.
            await _round_trip(AsyncSession.connection(session))
        except BaseException:
            await self._discard(unit)
            raise
        await super()._start(
            unit, session, {}, target, read_only=read_only, dialect=_NO_BEGIN_STATEMENTS.get(dialect, dialect)
        )

    def _refuse_overlapping(self, unit: UnitOfWork) -> None:
        waiting = current_state().held_units(self.datasource)
        for other in self._open:
            if other.owner_task is unit.owner_task or other.status in _ENDED or other in waiting:
                continue
            raise IllegalTransactionStateError(
                f"A new unit of work on datasource '{self.datasource}' would overlap {other.describe()} on the "
                "test's connection. In a test that rolls back (data_slice(rollback=True), @DataTest, "
                "RollbackTransaction) every unit of a datasource runs on that one connection, as a savepoint of "
                "the test's transaction, so units of tasks that run at the same time (asyncio.gather of "
                "@transactional calls or of repository calls, a background task started during the test) "
                "cannot share it: run them one after another.",
                datasource=self.datasource,
            )

    async def rollback(self, unit: UnitOfWork) -> None:
        """Roll back to the unit's savepoint; a read auto unit that ends healthy releases its savepoint instead.

        A read auto unit ends with a rollback in production, which undoes nothing: it wrote nothing. Here its
        savepoint also holds the savepoints of the units its task completed while it stayed open (the saves in
        the body of a loop over a stream read outside a transaction), and rolling it back would undo their
        writes, with no error. It is released instead, unless a statement failed in it (on PostgreSQL the
        transaction then waits for the rollback to the savepoint) or its state is unknown.
        """
        if unit.auto and unit.read_only and not unit.poisoned and not unit.rollback_only:
            session: AsyncSession = unit.resource
            if _adapter._transaction_active(unit):
                session.expunge_all()  # as the rollback does: what the read returned keeps its loaded state
                try:
                    await close_open_stream(unit)
                    await AsyncSession.commit(session)  # RELEASE SAVEPOINT: the unit wrote nothing
                    return
                except Exception:  # noqa: BLE001 — the rollback below ends the unit, as for any other
                    _logger.warning(
                        "rollback_transaction_read_unit_release_failed",
                        extra={"datasource": self.datasource, "unit": unit.describe()},
                        exc_info=True,
                    )
        try:
            await super().rollback(unit)
        finally:
            self._fail_foreign_units(unit)

    async def create_savepoint(self, unit: UnitOfWork) -> AsyncSessionTransaction:
        """The ``SAVEPOINT`` of a ``NESTED`` scope, opened as the datasource's own manager opens it, with its round
        trips (and those of its release and rollback) run apart (:class:`_ShieldedTransaction`)."""
        async with unit.operation():
            savepoint = _ShieldedTransaction(unit.resource, nested=True)
            await savepoint
            track_savepoint(unit, savepoint)
        # The template counts this savepoint once this returns: it is at the next depth.
        unit.attributes.setdefault(_adapter._TEMPLATE_SAVEPOINTS, []).append(
            (savepoint.sync_transaction, unit.savepoint_depth + 1)
        )
        return savepoint

    def _note_foreign_statement(
        self, conn: Connection, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        """``before_cursor_execute`` listener of the test's connection: record the unit that runs a statement
        inside the savepoint of the innermost unit when that unit belongs to another task, which the running
        task does not wait for (it is not among the units the running task holds)."""
        innermost = next((unit for unit in reversed(self._open) if unit.status not in _ENDED), None)
        if innermost is None or innermost.owner_task is _issuing_task():
            return
        state = current_state()
        issuer = state.target(self.datasource)
        if issuer is None or issuer is innermost or issuer not in self._open:
            return
        if innermost in state.held_units(self.datasource) or statement.lstrip().upper().startswith(
            _SAVEPOINT_STATEMENTS
        ):
            return
        foreign: list[UnitOfWork] = innermost.attributes.setdefault(_FOREIGN_UNITS, [])
        if issuer not in foreign:
            foreign.append(issuer)

    def _fail_foreign_units(self, unit: UnitOfWork) -> None:
        """*unit* rolled back to its savepoint: mark rollback-only each unit whose statements ran inside it."""
        for issuer in unit.attributes.pop(_FOREIGN_UNITS, ()):
            if issuer.completed:
                continue
            issuer.set_rollback_only(
                IllegalTransactionStateError(
                    f"{issuer.describe()} ran a statement while {unit.describe()} was open on the test's "
                    "connection, and that task's unit rolled back. In a test that rolls back (data_slice("
                    "rollback=True), @DataTest, RollbackTransaction) every unit of a datasource runs on that one "
                    "connection, as a savepoint of the test's transaction, so the statement ran inside that unit's "
                    "savepoint and its rollback undid it; in production each unit has a connection of its own. "
                    "Wait for the task (await it) before the unit that started it goes on.",
                    datasource=self.datasource,
                ),
                depth=0,
            )

    async def _close(self, unit: UnitOfWork, *, invalidate: bool) -> None:
        # Never discarded for a unit (one that failed to start after a cancellation included): that would end the
        # test's transaction. A poisoned unit rolled back to its savepoint; its cancelled round trip had ended.
        try:
            await super()._close(unit, invalidate=False)
        finally:
            if unit in self._open:
                self._open.remove(unit)
            if self._lost_in is None and self._connection_lost():
                self._lost_in = unit.describe()

    def __repr__(self) -> str:
        return f"RollbackTransactionManager(datasource={self.datasource!r}, original={self._original!r})"


def _read_streams_in_full(state: ORMExecuteState) -> Result[Any] | None:
    """Run a streamed statement (``session.stream()``, ``stream_scalars()``, a repository's ``stream_all()``) of a
    test's unit with its rows read when it runs, instead of through a server-side cursor.

    The test's units share one connection. A cursor left open there while the stream is read would break the
    other units' statements on MySQL and MariaDB, where a connection has one active result at a time (the
    driver reads the rest of the result into the next statement's reply, and the connection hangs), and on
    SQLite the rows a unit writes meanwhile would show up later in the scan. Read at once, the stream holds the
    rows of its statement's start, as on a connection of its own.
    """
    if not state.execution_options.get("stream_results"):
        return None
    return state.invoke_statement(execution_options={"stream_results": False})
