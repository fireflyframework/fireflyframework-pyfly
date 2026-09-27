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
"""The ``AsyncSession`` classes of the relational unit of work, and the ``SessionProvider`` bean.

- :class:`UnitSession` is the session of a unit the transaction manager opened. Every operation runs under
  the unit's operation guard (a child task that shares the unit waits its turn instead of corrupting the
  session), refuses a completed unit, and records failures; so do the ``SAVEPOINT``, ``RELEASE`` and
  ``ROLLBACK TO SAVEPOINT`` of ``begin_nested()``, and every fetch of a streamed result
  (:class:`GuardedResult`). On MySQL and MariaDB, whose connection has one active result at a time, an open
  streamed result holds the unit until it is exhausted or closed: every other operation raises
  ``IllegalTransactionStateError`` meanwhile, and a stream still open when its unit completes, or when the
  savepoint it was opened in ends, is closed first (:func:`close_open_stream`). In a read-only unit
  (``read_only=True``, or the read
  auto unit of a ``find*``/``count*``/``exists*``/``stream*``/``get*`` repository method) a Core
  ``INSERT``/``UPDATE``/``DELETE`` is refused before it is sent, on every backend, as the ORM flush guard
  refuses ORM writes (a raw ``text()`` statement is not inspected). ``commit``, ``rollback`` and ``close``
  belong to the unit, so calling them raises.
- :class:`ScopedAsyncSession` is what the transient ``async_session`` bean hands out. Inside a unit of
  work for its datasource the unit-of-work API (``execute``, ``scalar``, ``scalars``, ``get``,
  ``get_one``, ``add``, ``add_all``, ``delete``, ``merge``, ``flush``, ``refresh``, ``stream``,
  ``stream_scalars``, ``begin_nested``, ``in_transaction``) delegates to the unit's session, and
  ``commit``/``rollback`` raise, as Spring's shared ``EntityManager`` does; outside a unit it is an
  ordinary session its owner commits and closes. A DAO that injects ``AsyncSession`` therefore joins
  ``@transactional``.
- :class:`SessionProvider` is the recommended injection for custom data access code: ``current()`` is the
  session of the current unit, and ``async with provider.unit(read_only=...)`` joins it or opens a short
  unit of its own.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.registry import PRIMARY, TransactionManagerRegistry, installed_registry, resolve_manager
from pyfly.data.transaction.template import infrastructure_unit
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = [
    "RELEASING_SAVEPOINT",
    "GuardedResult",
    "ScopedAsyncSession",
    "SessionProvider",
    "UnitSavepoint",
    "UnitSession",
    "close_open_stream",
    "track_savepoint",
    "unit_session_class",
]


# ---------------------------------------------------------------------------------------------------------
# The session of a unit
# ---------------------------------------------------------------------------------------------------------


RELEASING_SAVEPOINT = "pyfly_releasing_savepoint"
"""``UnitOfWork.attributes`` key: ``(task, SessionTransaction)`` of the savepoint the application is
releasing, set under the operation guard while the release runs. The transaction manager attributes a
failure of the flush that releasing runs, in that task, to that savepoint."""

_SAVEPOINT_HANDLES = "pyfly_savepoint_handles"
"""``UnitOfWork.attributes`` key: the handle of each open savepoint the unit tracks, by its
``SessionTransaction``."""


def track_savepoint(unit: UnitOfWork, savepoint: AsyncSessionTransaction) -> None:
    """Record *savepoint*, which the running task just opened on *unit*'s session under the unit's operation
    guard, as the unit's innermost savepoint (:meth:`UnitOfWork.savepoint_opened`), and report its end
    (:meth:`UnitOfWork.savepoint_closed`) however it ends: released, rolled back, or closed along with an
    enclosing savepoint or the session."""
    handles: dict[SessionTransaction, AsyncSessionTransaction] | None = unit.attributes.get(_SAVEPOINT_HANDLES)
    if handles is None:
        handles = unit.attributes[_SAVEPOINT_HANDLES] = {}
        tracked = handles

        def _ended(_session: Session, transaction: SessionTransaction) -> None:
            handle = tracked.pop(transaction, None)
            if handle is not None:
                unit.savepoint_closed(handle)

        event.listen(unit.resource.sync_session, "after_transaction_end", _ended)
    transaction = savepoint.sync_transaction
    if transaction is None:
        return  # it did not start: there is no savepoint to track
    handles[transaction] = savepoint
    unit.savepoint_opened(savepoint)


class _OpenStream:
    """A streamed result that holds its unit's connection until it is exhausted or closed, on a backend whose
    connection has one active result at a time (``UnitOfWork.stream_opened``)."""

    __slots__ = ("failed", "owner", "result", "statement")

    def __init__(self, statement: Any, result: Any) -> None:
        self.statement = statement
        self.result = result  # the streamed result as opened (the results derived from it share its cursor)
        self.owner = asyncio.current_task()
        self.failed = False  # a fetch failed in the driver: its state is unknown, so it is never read to its end

    def __str__(self) -> str:
        try:
            sql = " ".join(str(self.statement).split())
        except Exception:  # noqa: BLE001 — a statement that cannot render itself is still named by its type
            sql = type(self.statement).__name__
        if len(sql) > 160:
            sql = f"{sql[:157]}..."
        owner = repr(self.owner.get_name()) if self.owner is not None else "(none)"
        return f"a streamed result of task {owner} ({sql})"


def _cursor_released(result: Any) -> bool:
    """Whether the server-side cursor under *result* (a streamed result, or one derived from it) is released:
    its rows are exhausted or it was closed, so the connection can run another statement. SQLAlchemy
    soft-closes the ``CursorResult`` then (an ORM result keeps it as ``raw``)."""
    current = getattr(result, "_real_result", result)
    for _ in range(8):  # an ORM result wraps the cursor result once; this only bounds a pathological chain
        raw = getattr(current, "raw", None)
        if raw is None:
            break
        current = raw
    return getattr(current, "_soft_closed", False) is True


class GuardedResult:
    """A streamed result of a unit's session whose fetches run under the unit's operation guard.

    On a backend whose connection has one active result at a time it also holds the unit while its cursor is
    open (``UnitOfWork.stream_opened``): the results derived from it (``.scalars()``, ``.partitions()``)
    share that hold, and the first fetch that finds the rows exhausted, or :meth:`close`, releases it.
    """

    __slots__ = ("_result", "_stream", "_unit")

    def __init__(self, result: Any, unit: UnitOfWork, stream: _OpenStream | None = None) -> None:
        self._result = result
        self._unit = unit
        self._stream = stream

    def __aiter__(self) -> GuardedResult:
        return self

    async def __anext__(self) -> Any:
        async with self._unit.operation(stream=self._stream):
            try:
                return await self._result.__anext__()
            except BaseException as error:
                self._failed(error)
                raise
            finally:
                self._release_when_done()

    async def close(self) -> None:
        """Close the result; the rows not fetched yet are dropped (on MySQL and MariaDB they are read first,
        as the connection requires). Nothing is left to close once the rows are exhausted.

        Closing is cleanup, and it often runs in a task of its own: Python closes an async generator left
        unfinished (a ``break`` out of ``stream_all``) later, in a new task. So it never raises the unit's
        own refusals, and it leaves the cursor to the unit when the unit cannot take its close now: once the
        unit is completing (its ``COMMIT`` or ``ROLLBACK`` closes the stream first, under its guard:
        :func:`close_open_stream`), when the close is refused (the unit started completing while this waited
        for its guard, or another task holds a savepoint on it: the end of the unit, or of that savepoint,
        closes the stream), and when a cancellation or a driver error interrupted a fetch (the connection is
        in an unknown state, and the unit discards it). Until then the stream keeps holding the unit.
        """
        unit = self._unit
        stream = self._stream
        if _cursor_released(self._result):
            if stream is not None:
                unit.stream_closed(stream)
            return
        if unit.completed or unit.poisoned or (stream is not None and stream.failed):
            return
        try:
            async with unit.operation(stream=stream):
                try:
                    await self._result.close()
                except BaseException as error:
                    self._failed(error)
                    raise
        except IllegalTransactionStateError:
            return
        if stream is not None:
            unit.stream_closed(stream)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._result, name)
        if not callable(attribute):
            return attribute
        unit = self._unit
        stream = self._stream
        if inspect.iscoroutinefunction(attribute):

            async def guarded(*args: Any, **kwargs: Any) -> Any:
                async with unit.operation(stream=stream):
                    try:
                        return _wrap(await attribute(*args, **kwargs), unit, stream)
                    except BaseException as error:
                        self._failed(error)
                        raise
                    finally:
                        self._release_when_done()

            return guarded

        def plain(*args: Any, **kwargs: Any) -> Any:
            return _wrap(attribute(*args, **kwargs), unit, stream)

        return plain

    def _release_when_done(self) -> None:
        stream = self._stream
        if stream is not None and _cursor_released(self._result):
            self._unit.stream_closed(stream)

    def _failed(self, error: BaseException) -> None:
        """A fetch raised *error*: after a driver error or a cancellation the cursor's state on the connection
        is unknown, and the stream is never read to its end (``close_open_stream``)."""
        if self._stream is not None and (isinstance(error, DBAPIError) or not isinstance(error, Exception)):
            self._stream.failed = True


def _wrap(value: Any, unit: UnitOfWork, stream: _OpenStream | None) -> Any:
    """Keep a result derived from a streamed result (``.scalars()``, ``.partitions()``) guarded."""
    if hasattr(value, "__anext__") and not isinstance(value, GuardedResult):
        return GuardedResult(value, unit, stream)
    return value


def _opened(result: Any, unit: UnitOfWork, args: tuple[Any, ...], kwargs: dict[str, Any]) -> _OpenStream | None:
    """Record *result*, a streamed result just opened on *unit* under its guard, as the stream that holds the
    unit, when the unit's connection has one active result at a time and the result's cursor is open."""
    if unit.manager.capabilities.multiple_active_results or _cursor_released(result):
        return None
    stream = _OpenStream(args[0] if args else kwargs.get("statement"), result)
    unit.stream_opened(stream)
    return stream


async def close_open_stream(unit: UnitOfWork) -> None:
    """Close the streamed result that holds *unit* (``UnitOfWork.stream_opened``), if one is open; it runs
    under the unit's guard before the unit commits or rolls back, and before a savepoint ends (a stream open
    then was opened inside that savepoint: no savepoint opens while a stream is open).

    On MySQL and MariaDB a ``COMMIT`` or ``ROLLBACK`` cannot run while a result is open on the connection:
    closing it reads the rows not fetched yet and drops them (what the driver does before any other
    statement), which leaves the connection clean. A stream whose last fetch failed is only forgotten: its
    state is unknown, and the unit's rollback discards the connection if it cannot run.
    """
    stream = unit.open_stream
    if not isinstance(stream, _OpenStream):
        return
    unit.stream_closed(stream)
    if not stream.failed:
        await stream.result.close()


class UnitSession(AsyncSession):
    """The ``AsyncSession`` of a unit of work (see the module documentation)."""

    _pyfly_unit: UnitOfWork | None = None

    # -- guarded operations ----------------------------------------------------------------------------------

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().execute(*args, **kwargs)
        if unit.read_only:
            _refuse_dml(unit, args, kwargs)
        async with unit.operation():
            return await super().execute(*args, **kwargs)

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().scalar(*args, **kwargs)
        if unit.read_only:
            _refuse_dml(unit, args, kwargs)
        async with unit.operation():
            return await super().scalar(*args, **kwargs)

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().scalars(*args, **kwargs)
        if unit.read_only:
            _refuse_dml(unit, args, kwargs)
        async with unit.operation():
            return await super().scalars(*args, **kwargs)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().get(*args, **kwargs)
        async with unit.operation():
            return await super().get(*args, **kwargs)

    async def get_one(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().get_one(*args, **kwargs)
        async with unit.operation():
            return await super().get_one(*args, **kwargs)

    async def merge(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().merge(*args, **kwargs)
        async with unit.operation():
            return await super().merge(*args, **kwargs)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().delete(*args, **kwargs)
        async with unit.operation():
            return await super().delete(*args, **kwargs)

    async def flush(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().flush(*args, **kwargs)
        async with unit.operation():
            return await super().flush(*args, **kwargs)

    async def refresh(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().refresh(*args, **kwargs)
        async with unit.operation():
            return await super().refresh(*args, **kwargs)

    async def run_sync(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().run_sync(*args, **kwargs)
        async with unit.operation():
            return await super().run_sync(*args, **kwargs)

    async def connection(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().connection(*args, **kwargs)
        async with unit.operation():
            return await super().connection(*args, **kwargs)

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().stream(*args, **kwargs)
        async with unit.operation():
            result = await super().stream(*args, **kwargs)
            return GuardedResult(result, unit, _opened(result, unit, args, kwargs))

    async def stream_scalars(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().stream_scalars(*args, **kwargs)
        async with unit.operation():
            # AsyncSession.stream_scalars() goes through self.stream(), which would guard every fetch a
            # second time: open the stream unguarded here and guard its scalars once.
            result = await AsyncSession.stream(self, *args, **kwargs)
            stream = _opened(result, unit, args, kwargs)
        return GuardedResult(result.scalars(), unit, stream)

    def begin_nested(self) -> AsyncSessionTransaction:
        unit = self._pyfly_unit
        if unit is None:
            return super().begin_nested()
        return UnitSavepoint(self, unit)

    def add(self, instance: object, _warn: bool = True) -> None:
        unit = self._pyfly_unit
        if unit is not None:
            unit.check_usable()
            unit.check_savepoint_owner()  # the next flush would write it inside another task's savepoint
            # Its flush would be refused while a stream holds the unit, and the object would stay pending
            # until the unit's own flush at commit: refused now, the caller's failure is the whole outcome.
            unit.check_open_stream()
        super().add(instance, _warn=_warn)

    def add_all(self, instances: Any) -> None:
        unit = self._pyfly_unit
        if unit is not None:
            unit.check_usable()
            unit.check_savepoint_owner()
            unit.check_open_stream()
        super().add_all(instances)

    # -- completion belongs to the unit ------------------------------------------------------------------------

    async def commit(self) -> None:
        if self._pyfly_unit is not None:
            raise _completion_refused(self._pyfly_unit, "commit")
        await super().commit()

    async def rollback(self) -> None:
        if self._pyfly_unit is not None:
            raise _completion_refused(self._pyfly_unit, "rollback")
        await super().rollback()

    async def close(self) -> None:
        if self._pyfly_unit is not None:
            raise _completion_refused(self._pyfly_unit, "close")
        await super().close()


class UnitSavepoint(AsyncSessionTransaction):
    """A savepoint the application opens on a unit's session (``session.begin_nested()``): its
    ``SAVEPOINT``, ``RELEASE SAVEPOINT`` and ``ROLLBACK TO SAVEPOINT`` run under the unit's operation guard,
    like every other statement of the unit, and never across the code inside the block. A streamed result
    the block left open is closed before its savepoint ends (:func:`close_open_stream`).

    Releasing it flushes what the block left pending (``session.add()`` or ``merge()`` with no flush inside
    the block). When that flush fails, SQLAlchemy rolls the savepoint back, and at the end of an ``async
    with`` block also closes it, before the failure is raised: the failure went away with the savepoint and
    does not mark the unit, exactly as when the block flushes explicitly (see :data:`RELEASING_SAVEPOINT`).
    """

    __slots__ = ("_pyfly_unit",)

    def __init__(self, session: AsyncSession, unit: UnitOfWork) -> None:
        super().__init__(session, nested=True)
        self._pyfly_unit = unit

    async def start(self, is_ctxmanager: bool = False) -> AsyncSessionTransaction:
        unit = self._pyfly_unit
        async with unit.operation():  # refused while another task holds the unit's innermost savepoint
            started = await super().start(is_ctxmanager)
            track_savepoint(unit, self)
        return started

    async def commit(self) -> None:
        unit = self._pyfly_unit
        marker = (asyncio.current_task(), self.sync_transaction)
        try:
            async with unit.operation(stream=unit.open_stream):
                await close_open_stream(unit)  # one the block left open (no savepoint opens beside a stream)
                unit.attributes[RELEASING_SAVEPOINT] = marker
                await super().commit()
        finally:
            _released(unit, marker)

    async def rollback(self) -> None:
        unit = self._pyfly_unit
        async with unit.operation(stream=unit.open_stream):
            await close_open_stream(unit)
            await super().rollback()

    async def __aexit__(self, type_: Any, value: Any, traceback: Any) -> None:
        unit = self._pyfly_unit
        if type_ is not None:
            async with unit.operation(stream=unit.open_stream):
                await close_open_stream(unit)
                await super().__aexit__(type_, value, traceback)
            return
        marker = (asyncio.current_task(), self.sync_transaction)
        try:
            async with unit.operation(stream=unit.open_stream):
                await close_open_stream(unit)
                unit.attributes[RELEASING_SAVEPOINT] = marker
                await super().__aexit__(type_, value, traceback)
        finally:
            _released(unit, marker)


def _released(unit: UnitOfWork, marker: tuple[Any, Any]) -> None:
    """Forget the release marked by *marker* (the operation guard recorded its failure, if any, by now)."""
    if unit.attributes.get(RELEASING_SAVEPOINT) is marker:
        del unit.attributes[RELEASING_SAVEPOINT]


def _refuse_dml(unit: UnitOfWork, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Refuse a Core ``INSERT``/``UPDATE``/``DELETE`` in a read-only unit, before it reaches the database."""
    statement = args[0] if args else kwargs.get("statement")
    if not getattr(statement, "is_dml", False):
        return
    hint = (
        "A repository read method (find*, count*, exists*, stream*, get*) runs in a read-only auto unit; give a "
        "method that writes another name, or call it inside @transactional."
        if unit.auto
        else "Drop read_only=True from the boundary, or write in a unit of its own (Propagation.REQUIRES_NEW)."
    )
    raise IllegalTransactionStateError(
        f"{unit.describe()} is read-only and cannot run {type(statement).__name__.upper()} on "
        f"{getattr(getattr(statement, 'table', None), 'name', 'a table')}. {hint}",
        datasource=unit.datasource,
    )


def _completion_refused(unit: UnitOfWork, operation: str) -> IllegalTransactionStateError:
    return IllegalTransactionStateError(
        f"Cannot {operation} the session of {unit.describe()}: the unit of work completes it (a transactional "
        "boundary commits or rolls back on exit, and an auto unit when its repository call returns). Raise "
        "an exception to roll back, or use Propagation.REQUIRES_NEW / NESTED for work that commits on its own.",
        datasource=unit.datasource,
    )


_UNIT_SESSION_CLASSES: dict[type[AsyncSession], type[AsyncSession]] = {AsyncSession: UnitSession}


def unit_session_class(session_class: type[AsyncSession]) -> type[AsyncSession]:
    """The unit-session class for a session factory's class (a custom ``AsyncSession`` subclass keeps its
    behavior and gains the unit's guard)."""
    found = _UNIT_SESSION_CLASSES.get(session_class)
    if found is None:
        if issubclass(session_class, UnitSession):
            found = session_class
        else:
            found = type(f"Unit{session_class.__name__}", (UnitSession, session_class), {})
        _UNIT_SESSION_CLASSES[session_class] = found
    return found


# ---------------------------------------------------------------------------------------------------------
# The transient async_session bean
# ---------------------------------------------------------------------------------------------------------


class ScopedAsyncSession(AsyncSession):
    """An ``AsyncSession`` that joins the unit of work bound for its datasource (see the module
    documentation). Build one with :meth:`of`."""

    def __init__(self, *args: Any, pyfly_datasource: str = PRIMARY, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pyfly_datasource = pyfly_datasource

    @classmethod
    def of(cls, factory: async_sessionmaker[AsyncSession], *, datasource: str | None = None) -> ScopedAsyncSession:
        """A scoped session configured like *factory*'s sessions, for *datasource* (by default the datasource
        *factory* belongs to)."""
        if datasource is None:
            from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager

            datasource = SqlAlchemyTransactionManager.for_sessionmaker(factory).datasource
        return cls(pyfly_datasource=datasource, **factory.kw)

    @property
    def datasource(self) -> str:
        """The datasource whose unit of work this session joins."""
        return self._pyfly_datasource

    def _pyfly_delegate(self) -> AsyncSession | None:
        unit = current_state().target(self._pyfly_datasource)
        if unit is None or not isinstance(unit.resource, AsyncSession):
            return None
        unit.check_usable()
        return unit.resource

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.execute(*args, **kwargs) if target is not None else super().execute(*args, **kwargs))

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.scalar(*args, **kwargs) if target is not None else super().scalar(*args, **kwargs))

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.scalars(*args, **kwargs) if target is not None else super().scalars(*args, **kwargs))

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.get(*args, **kwargs) if target is not None else super().get(*args, **kwargs))

    async def get_one(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.get_one(*args, **kwargs) if target is not None else super().get_one(*args, **kwargs))

    async def merge(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.merge(*args, **kwargs) if target is not None else super().merge(*args, **kwargs))

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.delete(*args, **kwargs) if target is not None else super().delete(*args, **kwargs))

    async def flush(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.flush(*args, **kwargs) if target is not None else super().flush(*args, **kwargs))

    async def refresh(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.refresh(*args, **kwargs) if target is not None else super().refresh(*args, **kwargs))

    async def stream(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        return await (target.stream(*args, **kwargs) if target is not None else super().stream(*args, **kwargs))

    async def stream_scalars(self, *args: Any, **kwargs: Any) -> Any:
        target = self._pyfly_delegate()
        if target is not None:
            return await target.stream_scalars(*args, **kwargs)
        return await super().stream_scalars(*args, **kwargs)

    def add(self, instance: object, _warn: bool = True) -> None:
        target = self._pyfly_delegate()
        if target is not None:
            target.add(instance, _warn=_warn)
        else:
            super().add(instance, _warn=_warn)

    def add_all(self, instances: Any) -> None:
        target = self._pyfly_delegate()
        if target is not None:
            target.add_all(instances)
        else:
            super().add_all(instances)

    def begin_nested(self) -> Any:
        target = self._pyfly_delegate()
        return target.begin_nested() if target is not None else super().begin_nested()

    def in_transaction(self) -> bool:
        target = self._pyfly_delegate()
        return True if target is not None else super().in_transaction()

    async def commit(self) -> None:
        target = self._pyfly_delegate()
        if target is not None:
            raise _scoped_refused(self._pyfly_datasource, "commit")
        await super().commit()

    async def rollback(self) -> None:
        target = self._pyfly_delegate()
        if target is not None:
            raise _scoped_refused(self._pyfly_datasource, "rollback")
        await super().rollback()


def _scoped_refused(datasource: str, operation: str) -> IllegalTransactionStateError:
    return IllegalTransactionStateError(
        f"Cannot {operation} an injected AsyncSession inside a unit of work on datasource '{datasource}': it "
        "is the unit's session, and the unit completes it (Spring refuses getTransaction() on its shared "
        "EntityManager the same way).",
        datasource=datasource,
    )


# ---------------------------------------------------------------------------------------------------------
# SessionProvider
# ---------------------------------------------------------------------------------------------------------


class SessionProvider:
    """The session of the current unit of work, and programmatic short units, for custom data access code.

    ::

        class ReportDao:
            def __init__(self, sessions: SessionProvider) -> None:
                self._sessions = sessions

            async def totals(self) -> list[Row]:
                async with self._sessions.unit(read_only=True) as session:
                    return (await session.execute(text("SELECT ..."))).all()
    """

    def __init__(
        self,
        managers: TransactionManagerRegistry | Callable[[], TransactionManagerRegistry | None] | None = None,
        *,
        datasource: str = PRIMARY,
    ) -> None:
        # A callable is resolved at first use: the session_provider bean is built before the context's
        # transaction_manager_registry bean may be registered.
        self._managers = managers
        self._datasource = datasource

    def _registry(self) -> TransactionManagerRegistry | None:
        managers = self._managers
        if managers is None or isinstance(managers, TransactionManagerRegistry):
            return managers if managers is not None else installed_registry()
        resolved = managers()
        if resolved is not None:
            self._managers = resolved
        return resolved if resolved is not None else installed_registry()

    def current(self, datasource: str | None = None) -> AsyncSession | None:
        """The session of the unit bound for *datasource* now (a repository's operation scope counts), or
        ``None`` outside one."""
        unit = current_state().target(datasource or self._datasource)
        if unit is None or unit.completed or not isinstance(unit.resource, AsyncSession):
            return None
        return unit.resource

    def unit(
        self, *, read_only: bool = False, datasource: str | None = None, single_statement: bool = False
    ) -> AbstractAsyncContextManager[AsyncSession]:
        """Join the unit bound for *datasource*, or open a short unit that commits (or, read-only, ends
        without writing) when the block exits; yields its session."""
        name = datasource or self._datasource
        registry = self._registry()
        manager = registry.get(name) if registry is not None else resolve_manager(name)
        return infrastructure_unit(manager, read_only=read_only, single_statement=single_statement)
