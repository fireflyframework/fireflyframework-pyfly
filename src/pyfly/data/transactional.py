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
"""Unified declarative transaction management — one ``@transactional`` for every backend.

Like Spring's ``@Transactional``, the annotation is **uniform**; the transaction *manager* is
backend-specific and selected at call time from what the service exposes:

- a relational ``async_sessionmaker`` on ``self._session_factory`` → SQLAlchemy transaction
  (full propagation / isolation / read-only / rollback semantics), or
- a MongoDB client on ``self._motor_client`` → Mongo `ClientSession` transaction.

The backend adapters provide the execution (``run_relational_transaction`` /
``run_mongo_transaction``); they are imported lazily so this core module pulls in neither
SQLAlchemy nor pymongo.
"""

from __future__ import annotations

import enum
import functools
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class Propagation(enum.Enum):
    """Transaction propagation behaviour (relational backend)."""

    REQUIRED = "REQUIRED"
    REQUIRES_NEW = "REQUIRES_NEW"
    SUPPORTS = "SUPPORTS"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    NEVER = "NEVER"
    MANDATORY = "MANDATORY"


class Isolation(enum.Enum):
    """Transaction isolation level (relational backend)."""

    DEFAULT = "DEFAULT"
    READ_UNCOMMITTED = "READ UNCOMMITTED"
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


def transactional(
    func: F | None = None,
    /,
    *,
    propagation: Propagation = Propagation.REQUIRED,
    isolation: Isolation = Isolation.DEFAULT,
    read_only: bool = False,
    rollback_for: tuple[type[BaseException], ...] = (Exception,),
    no_rollback_for: tuple[type[BaseException], ...] = (),
) -> Any:
    """Declarative transaction management — the single annotation for **all** backends.

    Works bare (``@transactional``) or parametrized (``@transactional(propagation=...)``). At
    call time it dispatches to the transaction manager the service exposes:

    - **Relational** (``self._session_factory`` is an ``async_sessionmaker``): opens a session,
      applies ``propagation`` / ``isolation`` / ``read_only``, commits on success and rolls back
      per ``rollback_for`` / ``no_rollback_for``, and patches the service's repositories onto the
      active session.
    - **Document** (``self._motor_client`` is a pymongo ``AsyncMongoClient``): opens a Mongo session +
      transaction, injects it as the ``session`` kwarg, commits on success and aborts per
      ``rollback_for`` / ``no_rollback_for``. (``propagation`` / ``isolation`` / ``read_only`` are
      relational concepts and are ignored on the document backend.)

    Raises ``RuntimeError`` if the service exposes neither manager.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self_arg = args[0] if args else None

            if getattr(self_arg, "_session_factory", None) is not None:
                from pyfly.data.relational.sqlalchemy.transactional import run_relational_transaction

                return await run_relational_transaction(
                    fn,
                    args,
                    kwargs,
                    propagation=propagation,
                    isolation=isolation,
                    read_only=read_only,
                    rollback_for=rollback_for,
                    no_rollback_for=no_rollback_for,
                )

            if getattr(self_arg, "_motor_client", None) is not None:
                from pyfly.data.document.mongodb.transactional import run_mongo_transaction

                return await run_mongo_transaction(
                    fn, args, kwargs, rollback_for=rollback_for, no_rollback_for=no_rollback_for
                )

            raise RuntimeError(
                f"{fn.__qualname__}: @transactional found no transaction manager on the service. "
                "Expose a relational '_session_factory' (async_sessionmaker) or a document "
                "'_motor_client' (pymongo AsyncMongoClient)."
            )

        wrapper.__pyfly_transactional__ = True  # type: ignore[attr-defined]
        wrapper.__pyfly_propagation__ = propagation  # type: ignore[attr-defined]
        wrapper.__pyfly_isolation__ = isolation  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator(func) if func is not None else decorator
