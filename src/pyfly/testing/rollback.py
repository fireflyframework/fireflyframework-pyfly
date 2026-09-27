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
"""A test's units of work in one transaction per datasource that rolls back when the test ends (Spring's
``@DataJpaTest`` / ``@Transactional`` test rollback).

::

    async with await data_slice(UserRepository, config=config) as ctx:
        async with RollbackTransaction(ctx):
            await ctx.get_bean(UserRepository).save(User(email="a@example.com"))
        # nothing was committed

``data_slice(..., rollback=True)`` and ``@DataTest`` wrap every test in one. On entry, each covered datasource
(the default one unless *datasources* names others) gets a connection with a transaction open on it, and its
transaction manager is replaced, in the context's ``TransactionManagerRegistry``, by one that runs every unit
of work of the test as a savepoint of that transaction. Repository calls, ``@transactional`` services,
``SessionProvider`` units, the ``AsyncSession`` bean inside a unit and the framework stores all find it.
Each unit still completes on its own (its savepoint is released or rolled back, and its after-commit
callbacks run), so a failing unit leaves the test's earlier writes in place, on PostgreSQL too. On exit the
datasource's own manager is back and the transaction rolls back.

The units of the task that entered the block, and of the tasks it starts, take part; the context's own
background work (started before the block) keeps running on the datasource's own connections and commits.
What differs from production, by construction:

- every unit of a datasource runs on one connection: units that overlap in time (tasks that each open a
  unit of their own and run at the same time) cannot share it, so run them one after another. A unit that
  starts while another task's unit is open there is refused with ``IllegalTransactionStateError`` before
  it touches the connection, and the test's transaction goes on;
- ``REQUIRES_NEW`` gets a savepoint too, so the outer unit's rollback undoes it;
- the settings of a transaction are the test transaction's: a unit's isolation level, read-only hint and
  SQLite ``BEGIN IMMEDIATE`` are not applied (a read-only unit still refuses ORM writes), and what a unit sets
  with ``SET LOCAL`` (a PostgreSQL statement timeout, an after-begin customizer's setting) lasts until the
  test ends unless the unit rolls back. The test's transaction is a real one on an application's own engine
  too: a plain SQLite engine gets its ``BEGIN`` from the test's connection, and an ``AUTOCOMMIT`` engine the
  database's default isolation level;
- DDL inside the test commits on MySQL and MariaDB (their DDL ends the transaction), and a session the
  application opens itself outside every unit (``async with factory() as session``) commits for real.

Only relational datasources roll back; a document datasource (MongoDB) is left alone.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncTransaction

    from pyfly.data.transaction.manager import TransactionManager
    from pyfly.data.transaction.registry import TransactionManagerRegistry

__all__ = ["RollbackTransaction", "active_rollback_transaction"]

_ACTIVE: ContextVar[RollbackTransaction | None] = ContextVar("pyfly_rollback_transaction", default=None)


def active_rollback_transaction() -> RollbackTransaction | None:
    """The rollback transaction the running task takes part in, or ``None``."""
    return _ACTIVE.get()


class RollbackTransaction:
    """Runs the units of work of the entering task in transactions that roll back on exit (see the module
    documentation).

    *target* is a started ``ApplicationContext`` (its ``transaction_manager_registry`` bean) or a
    ``TransactionManagerRegistry``. *datasources* names the datasources that roll back (default: the
    registry's default datasource, when one is configured). A named datasource without a relational manager
    raises ``LookupError``.
    """

    def __init__(self, target: Any, *, datasources: Iterable[str] | None = None) -> None:
        self._target = target
        self._names = list(datasources) if datasources is not None else None
        self._held: list[tuple[TransactionManagerRegistry, TransactionManager, Any, AsyncConnection, AsyncTransaction]]
        self._held = []
        self._token: Token[RollbackTransaction | None] | None = None

    def _registry(self) -> TransactionManagerRegistry:
        from pyfly.data.transaction.registry import TransactionManagerRegistry

        if isinstance(self._target, TransactionManagerRegistry):
            return self._target
        registry = self._target.get_bean(TransactionManagerRegistry)
        if not isinstance(registry, TransactionManagerRegistry):
            raise TypeError(f"Expected a TransactionManagerRegistry, got {type(registry).__name__}")
        return registry

    def connection(self, datasource: str | None = None) -> AsyncConnection:
        """The test's connection to *datasource* (the first covered one by default): statements on it see
        what the test's units wrote."""
        for _registry, original, _manager, connection, _transaction in self._held:
            if datasource is None or original.datasource == datasource:
                return connection
        raise LookupError(f"Datasource {datasource!r} does not roll back in this transaction")

    @contextlib.contextmanager
    def taking_part(self) -> Iterator[Self]:
        """Make the code of the block, and the tasks it starts, take part in this transaction.

        The task that entered the transaction takes part already; this is for code that runs in another
        context, such as a test body whose runner does not carry the context variables an async fixture set
        over to the test (the ``data_context`` fixture of PyFly's pytest plugin uses it). The transaction must
        have begun, and must outlive the block."""
        if self._token is None:
            raise RuntimeError("The rollback transaction is not running (enter it with 'async with' first)")
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            try:
                _ACTIVE.reset(token)
            except ValueError:  # left in another context than the one that entered
                _ACTIVE.set(None)

    async def __aenter__(self) -> Self:
        from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager

        registry = self._registry()
        explicit = self._names is not None
        names = self._names if self._names is not None else [registry.default_name]
        try:
            for name in names:
                original = registry.find(name)
                if not isinstance(original, SqlAlchemyTransactionManager):
                    if explicit:
                        raise LookupError(
                            f"Datasource {name!r} has no relational transaction manager to roll back "
                            f"(known: {registry.names()})"
                        )
                    continue
                await self._hold(registry, original)
        except BaseException:
            await self._release()
            raise
        self._token = _ACTIVE.set(self)
        return self

    async def _hold(self, registry: TransactionManagerRegistry, original: Any) -> None:
        from pyfly.data.relational.sqlalchemy.rollback import RollbackTransactionManager

        connection = await original.engine.connect()
        try:
            transaction = await _begin(connection)
        except BaseException:
            await connection.close()
            raise
        manager = RollbackTransactionManager(original, connection, lambda: _ACTIVE.get() is self)
        registry.register(manager)
        self._held.append((registry, original, manager, connection, transaction))

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if self._token is not None:
            try:
                _ACTIVE.reset(self._token)
            except ValueError:  # exited in another context than the one that entered (a fixture's teardown)
                _ACTIVE.set(None)
            self._token = None
        await self._release()

    async def _release(self) -> None:
        while self._held:
            registry, original, manager, connection, transaction = self._held.pop()
            if registry.unregister(manager):
                registry.register(original)
            try:
                if transaction.is_active:
                    await transaction.rollback()
            finally:
                await connection.close()


async def _begin(connection: AsyncConnection) -> AsyncTransaction:
    """Begin the test's transaction on *connection*, and make sure the database holds it open.

    An engine that runs in ``AUTOCOMMIT`` (an application's own) would send no ``BEGIN``: the test's
    transaction runs at the database's default isolation level instead. On SQLite, a plain pysqlite or
    aiosqlite engine (without PyFly's ``BEGIN`` recipe) defers its ``BEGIN`` until the first write, so the
    first unit's ``SAVEPOINT`` would start the transaction and its ``RELEASE SAVEPOINT`` commit it: the
    ``BEGIN`` is sent here.
    """
    from pyfly.data.relational.dialect_customizers import is_autocommit

    sync_connection = connection.sync_connection
    if sync_connection is not None and is_autocommit(sync_connection):
        await connection.execution_options(isolation_level=connection.default_isolation_level)
    transaction = await connection.begin()
    if connection.dialect.name == "sqlite":
        raw = await connection.get_raw_connection()
        if not getattr(raw.driver_connection, "in_transaction", False):
            await connection.exec_driver_sql("BEGIN")
    return transaction
