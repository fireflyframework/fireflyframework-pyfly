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

import dataclasses
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from pyfly.data.relational.datasource_registry import DataSource
from pyfly.data.relational.sqlalchemy import transaction_manager as _adapter
from pyfly.data.relational.sqlalchemy.session import UnitSession, unit_session_class
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.manager import TransactionCapabilities
from pyfly.data.transaction.unit_of_work import UnitOfWork, UnitStatus

# The dialect statements a unit issues right after BEGIN (MySQL's SET TRANSACTION READ ONLY) set the
# characteristics of a transaction that has not started yet: inside the test's transaction they fail.
_NO_BEGIN_STATEMENTS = {"mysql": "mysql (savepoint)", "mariadb": "mariadb (savepoint)"}

# A unit in one of these states has released (or rolled back to) its savepoint: it no longer holds the
# connection, even before its session closes.
_ENDED = (UnitStatus.COMMITTED, UnitStatus.ROLLED_BACK)


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
    tasks it started (they see its unit) is not refused.

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

    async def _close(self, unit: UnitOfWork, *, invalidate: bool) -> None:
        try:
            await super()._close(unit, invalidate=invalidate)
        finally:
            if unit in self._open:
                self._open.remove(unit)

    def __repr__(self) -> str:
        return f"RollbackTransactionManager(datasource={self.datasource!r}, original={self._original!r})"
