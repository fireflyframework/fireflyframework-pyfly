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
"""SQLAlchemy entry points of the unified transaction management.

``@transactional``, ``Propagation`` and ``Isolation`` are the backend-neutral ones of
:mod:`pyfly.data.transaction`, re-exported here for backward compatibility. This module adds:

- :func:`reactive_transactional`, the explicit-session decorator: the function receives the unit's
  ``AsyncSession`` as its first argument. It is a thin wrapper over the
  :class:`~pyfly.data.transaction.template.TransactionTemplate`, so it binds its unit: ``@transactional``
  code called inside it joins, and ``MANDATORY``/``NEVER`` see it.
- ``_active_session_var``, kept as a compatible read-only view of the unit bound to the running task (it
  was the ``ContextVar`` the previous implementation bound a session in).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction.context import current_state
from pyfly.data.transaction.decorator import transactional
from pyfly.data.transaction.definition import Isolation, Propagation, TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.template import TransactionBoundary
from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = [
    "Isolation",
    "Propagation",
    "reactive_transactional",
    "transactional",
]

F = TypeVar("F", bound=Callable[..., Any])


class _ActiveSessionView:
    """The ``AsyncSession`` of the innermost relational unit bound to the running task, read like the
    ``ContextVar`` it replaces (``_active_session_var.get()``). Binding goes through the unit of work now, so
    ``set``/``reset`` are refused."""

    name = "_active_session_var"

    def get(self, default: AsyncSession | None = None) -> AsyncSession | None:
        """The session of the innermost transactional relational unit, or *default*."""
        for _datasource, bound in reversed(current_state().units):
            if isinstance(bound, UnitOfWork) and not bound.completed and isinstance(bound.resource, AsyncSession):
                return bound.resource
        return default

    def set(self, value: object) -> Any:
        """Refused: bind a unit with ``@transactional`` or a ``TransactionTemplate`` instead."""
        raise IllegalTransactionStateError(
            "_active_session_var is a read-only view now; bind a unit with @transactional or "
            "TransactionTemplate(...).transaction()"
        )

    def reset(self, token: object) -> None:
        """Refused, like :meth:`set`."""
        self.set(token)

    def __repr__(self) -> str:
        return "<_active_session_var view of the bound unit of work>"


_active_session_var = _ActiveSessionView()


def reactive_transactional(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[F], F]:
    """Run the decorated coroutine function in a unit of work on *session_factory*'s datasource, passing the
    unit's ``AsyncSession`` as its first argument.

    It is a ``REQUIRED`` boundary: inside a unit on the same datasource it joins (and receives that unit's
    session); otherwise it begins a unit that commits on success and rolls back on an exception. The unit
    is bound, so ``@transactional`` methods called inside it join it.

    Usage::

        @reactive_transactional(session_factory)
        async def create_user(session: AsyncSession) -> User:
            user = User(name="Alice")
            session.add(user)
            return user
    """

    def decorator(func: F) -> F:
        if not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"@reactive_transactional needs an `async def` function, and "
                f"{getattr(func, '__qualname__', func)!r} is not a coroutine function"
            )
        coroutine: Callable[..., Coroutine[Any, Any, Any]] = func
        definition = TransactionDefinition(propagation=Propagation.REQUIRED)

        @functools.wraps(coroutine)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            manager = SqlAlchemyTransactionManager.for_sessionmaker(session_factory)
            async with TransactionBoundary(manager, definition) as unit:
                assert unit is not None  # REQUIRED always runs in a unit
                return await coroutine(unit.resource, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
