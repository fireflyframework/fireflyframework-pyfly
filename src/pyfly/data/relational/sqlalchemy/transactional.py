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
"""SQLAlchemy (relational) execution for the unified ``@transactional`` decorator.

The public ``@transactional`` annotation is the backend-neutral one in
:mod:`pyfly.data.transactional` (re-exported here for backward compatibility). This module
provides the relational *runner* it dispatches to (``run_relational_transaction``) plus
``reactive_transactional`` for explicit-session use. ``Propagation`` / ``Isolation`` are
re-exported from the core module.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pyfly.data.relational.routing import read_only as _read_only_scope
from pyfly.data.transactional import Isolation, Propagation, transactional

__all__ = [
    "Isolation",
    "Propagation",
    "reactive_transactional",
    "run_relational_transaction",
    "transactional",
]

F = TypeVar("F", bound=Callable[..., Any])

_active_session_var: ContextVar[AsyncSession | None] = ContextVar(
    "_active_session_var",
    default=None,
)


def _patch_repositories(self_arg: Any, session: AsyncSession) -> None:
    from pyfly.data.relational.sqlalchemy.repository import Repository

    for value in vars(self_arg).values():
        if isinstance(value, Repository):
            value._session = session
        elif hasattr(value, "__dict__"):
            # Patch nested services' repositories (one level deep)
            for nested_val in vars(value).values():
                if isinstance(nested_val, Repository):
                    nested_val._session = session


def _resolve_session_factory(self_arg: Any) -> async_sessionmaker[AsyncSession] | None:
    factory: async_sessionmaker[AsyncSession] | None = getattr(self_arg, "_session_factory", None)
    return factory


async def run_relational_transaction(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    propagation: Propagation = Propagation.REQUIRED,
    isolation: Isolation = Isolation.DEFAULT,
    read_only: bool = False,
    rollback_for: tuple[type[BaseException], ...] = (Exception,),
    no_rollback_for: tuple[type[BaseException], ...] = (),
) -> Any:
    """Execute *func* inside a SQLAlchemy transaction (the relational arm of ``@transactional``).

    Resolves ``async_sessionmaker`` from ``self._session_factory`` and uses a ``ContextVar`` for
    propagation semantics modelled after Spring's ``@Transactional``.
    """
    self_arg = args[0] if args else None
    existing: AsyncSession | None = _active_session_var.get()

    if propagation is Propagation.NEVER:
        if existing is not None:
            raise RuntimeError("Propagation.NEVER — active transaction exists")
        return await func(*args, **kwargs)

    if propagation is Propagation.NOT_SUPPORTED:
        token = _active_session_var.set(None)
        try:
            return await func(*args, **kwargs)
        finally:
            _active_session_var.reset(token)

    if propagation is Propagation.SUPPORTS:
        return await func(*args, **kwargs)

    if propagation is Propagation.MANDATORY:
        if existing is None:
            raise RuntimeError("Propagation.MANDATORY — no active transaction")
        return await func(*args, **kwargs)

    if propagation is Propagation.REQUIRED and existing is not None:
        return await func(*args, **kwargs)

    session_factory = _resolve_session_factory(self_arg) if self_arg is not None else None
    if session_factory is None:
        raise RuntimeError(
            "No _session_factory available on self — ensure the service has an injected async_sessionmaker"
        )

    execution_options: dict[str, Any] = {}
    if isolation is not Isolation.DEFAULT:
        execution_options["isolation_level"] = isolation.value

    # read_only routes to the replica when the session factory is a RoutingSessionFactory
    # (pyfly.data.relational.routing) and flags the session.
    ro_scope = _read_only_scope() if read_only else contextlib.nullcontext()
    with ro_scope:
        async with session_factory() as session:
            if read_only:
                session.info["read_only"] = True
            if execution_options:
                session = session.execution_options(**execution_options)  # type: ignore[attr-defined]
            await session.begin()
            token = _active_session_var.set(session)
            if self_arg is not None:
                _patch_repositories(self_arg, session)
            try:
                result = await func(*args, **kwargs)
                await session.commit()
                return result
            except BaseException as exc:
                # A BaseException that is not an Exception (CancelledError, KeyboardInterrupt,
                # SystemExit) always rolls back, regardless of rollback_for.
                if not isinstance(exc, Exception):
                    await session.rollback()
                elif isinstance(exc, tuple(no_rollback_for)):
                    await session.commit()
                elif isinstance(exc, tuple(rollback_for)):
                    await session.rollback()
                else:
                    await session.commit()
                raise
            finally:
                _active_session_var.reset(token)


def reactive_transactional(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[F], F]:
    """Decorator for declarative async transaction management with an explicit session factory.

    Wraps an async function in a database transaction. The decorated function receives an
    ``AsyncSession`` as its first argument. On success the transaction is committed; on exception
    it is rolled back and the exception re-raised.

    Usage::

        @reactive_transactional(session_factory)
        async def create_user(session: AsyncSession) -> User:
            user = User(name="Alice")
            session.add(user)
            return user
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            async with session_factory() as session, session.begin():
                result = await func(session, *args, **kwargs)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator
