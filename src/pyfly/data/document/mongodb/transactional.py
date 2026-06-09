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
"""MongoDB execution for the unified ``@transactional`` decorator.

Use the backend-neutral ``@transactional`` from :mod:`pyfly.data` on document services too — it
dispatches here when the service exposes a ``_motor_client``. ``mongo_transactional`` is kept as a
**deprecated** alias of ``@transactional`` for backward compatibility.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyfly.data.transactional import transactional


async def run_mongo_transaction(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    rollback_for: tuple[type[BaseException], ...] = (Exception,),
    no_rollback_for: tuple[type[BaseException], ...] = (),
) -> Any:
    """Execute *func* inside a MongoDB transaction (the document arm of ``@transactional``).

    Resolves the Mongo client (pymongo ``AsyncMongoClient``) from ``self._motor_client``, opens a
    session + transaction, injects the session as the ``session`` keyword argument, and commits on
    success. On error it aborts —
    except for exception types in ``no_rollback_for`` (or any ``Exception`` not in ``rollback_for``),
    which commit and then re-raise, mirroring the relational ``@transactional`` semantics. A
    ``BaseException`` that is not an ``Exception`` (cancellation/shutdown) always aborts.
    """
    self_arg = args[0] if args else None
    mongo_client = getattr(self_arg, "_motor_client", None)
    if mongo_client is None:
        raise RuntimeError(
            f"{func.__qualname__}: cannot resolve the Mongo client (pymongo AsyncMongoClient). "
            "Ensure the service has a '_motor_client' attribute."
        )

    # pymongo's AsyncMongoClient.start_session() is synchronous (returns an AsyncClientSession that
    # is itself an async context manager) — unlike Motor's coroutine variant, so no `await` here.
    async with mongo_client.start_session() as session:
        kwargs["session"] = session
        # session.start_transaction() commits on a clean context exit and aborts when an exception
        # escapes it. To honour no_rollback_for we let such exceptions exit cleanly (commit) and
        # re-raise them afterwards, rather than driving abort/commit manually (which would depend
        # on the driver's lazy-vs-eager start_transaction semantics).
        deferred: BaseException | None = None
        result: Any = None
        async with session.start_transaction():
            try:
                result = await func(*args, **kwargs)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise  # cancellation/shutdown -> abort
                if isinstance(exc, tuple(no_rollback_for)):
                    deferred = exc  # commit, then surface
                elif isinstance(exc, tuple(rollback_for)):
                    raise  # -> abort
                else:
                    deferred = exc  # not in rollback_for -> commit, then surface
        if deferred is not None:
            raise deferred
        return result


# Backward-compatibility alias. Prefer the unified `@transactional` from `pyfly.data`.
mongo_transactional = transactional
"""Deprecated alias of :func:`pyfly.data.transactional.transactional`. Use ``@transactional``."""
