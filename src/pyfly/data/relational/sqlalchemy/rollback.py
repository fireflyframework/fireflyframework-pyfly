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
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, Result
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import ORMExecuteState

from pyfly.data.relational.datasource_registry import DataSource
from pyfly.data.relational.sqlalchemy import transaction_manager as _adapter
from pyfly.data.relational.sqlalchemy.session import UnitSession, close_open_stream, unit_session_class
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.unit_of_work import UnitOfWork, UnitStatus

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


class RollbackTransactionManager(SqlAlchemyTransactionManager):
    """Serves one datasource's units of work, while a rollback transaction runs, as savepoints of the test's
    transaction on *connection*.

    Every unit gets a session bound to *connection* with ``join_transaction_mode="create_savepoint"``: its
    ``BEGIN`` is a ``SAVEPOINT``, its commit a ``RELEASE SAVEPOINT`` and its rollback a ``ROLLBACK TO
    SAVEPOINT``, so each unit still completes on its own. The settings that belong to a transaction (the
    isolation level, ``BEGIN IMMEDIATE``, a read-only transaction, an autocommit connection) are the test
    transaction's: a unit's own are not applied; a read-only unit keeps refusing ORM writes. A unit whose
    operation was cancelled rolls back to its savepoint instead of discarding the connection, which would
    end the test's transaction.

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
        return await super().begin(definition)

    async def open_auto_unit(self, *, read_only: bool, autocommit: bool | None = None) -> UnitOfWork:
        if not self._serves():
            return await self._original.open_auto_unit(read_only=read_only, autocommit=autocommit)
        return await super().open_auto_unit(read_only=read_only, autocommit=False)

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
        session = unit_session_class(factory.class_)(**options)
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

    def _note_foreign_statement(
        self, conn: Connection, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        """``before_cursor_execute`` listener of the test's connection: record the unit that runs a statement
        inside the savepoint of the innermost unit when that unit belongs to another task, which the running
        task does not wait for (it is not among the units the running task holds)."""
        innermost = next((unit for unit in reversed(self._open) if unit.status not in _ENDED), None)
        if innermost is None or innermost.owner_task is asyncio.current_task():
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
        try:
            await super()._close(unit, invalidate=invalidate)
        finally:
            if unit in self._open:
                self._open.remove(unit)

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
