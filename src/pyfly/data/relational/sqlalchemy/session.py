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
  session), refuses a completed unit, and records failures; ``commit``, ``rollback`` and ``close`` belong
  to the unit, so calling them raises.
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

import inspect
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.registry import PRIMARY, TransactionManagerRegistry, installed_registry, resolve_manager
from pyfly.data.transaction.template import infrastructure_unit
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = ["GuardedResult", "ScopedAsyncSession", "SessionProvider", "UnitSession", "unit_session_class"]


# ---------------------------------------------------------------------------------------------------------
# The session of a unit
# ---------------------------------------------------------------------------------------------------------


class GuardedResult:
    """A streamed result of a unit's session whose fetches run under the unit's operation guard."""

    __slots__ = ("_result", "_unit")

    def __init__(self, result: Any, unit: UnitOfWork) -> None:
        self._result = result
        self._unit = unit

    def __aiter__(self) -> GuardedResult:
        return self

    async def __anext__(self) -> Any:
        async with self._unit.operation():
            return await self._result.__anext__()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._result, name)
        if not callable(attribute):
            return attribute
        unit = self._unit
        if inspect.iscoroutinefunction(attribute):

            async def guarded(*args: Any, **kwargs: Any) -> Any:
                async with unit.operation():
                    return _wrap(await attribute(*args, **kwargs), unit)

            return guarded

        def plain(*args: Any, **kwargs: Any) -> Any:
            return _wrap(attribute(*args, **kwargs), unit)

        return plain


def _wrap(value: Any, unit: UnitOfWork) -> Any:
    """Keep a result derived from a streamed result (``.scalars()``, ``.partitions()``) guarded."""
    if hasattr(value, "__anext__") and not isinstance(value, GuardedResult):
        return GuardedResult(value, unit)
    return value


class UnitSession(AsyncSession):
    """The ``AsyncSession`` of a unit of work (see the module documentation)."""

    _pyfly_unit: UnitOfWork | None = None

    # -- guarded operations ----------------------------------------------------------------------------------

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().execute(*args, **kwargs)
        async with unit.operation():
            return await super().execute(*args, **kwargs)

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().scalar(*args, **kwargs)
        async with unit.operation():
            return await super().scalar(*args, **kwargs)

    async def scalars(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().scalars(*args, **kwargs)
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
            return GuardedResult(await super().stream(*args, **kwargs), unit)

    async def stream_scalars(self, *args: Any, **kwargs: Any) -> Any:
        unit = self._pyfly_unit
        if unit is None:
            return await super().stream_scalars(*args, **kwargs)
        async with unit.operation():
            return GuardedResult(await super().stream_scalars(*args, **kwargs), unit)

    def add(self, instance: object, _warn: bool = True) -> None:
        unit = self._pyfly_unit
        if unit is not None:
            unit.check_usable()
        super().add(instance, _warn=_warn)

    def add_all(self, instances: Any) -> None:
        unit = self._pyfly_unit
        if unit is not None:
            unit.check_usable()
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

    def __init__(self, managers: TransactionManagerRegistry | None = None, *, datasource: str = PRIMARY) -> None:
        self._managers = managers
        self._datasource = datasource

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
        registry = self._managers if self._managers is not None else installed_registry()
        manager = registry.get(name) if registry is not None else resolve_manager(name)
        return infrastructure_unit(manager, read_only=read_only, single_statement=single_statement)
