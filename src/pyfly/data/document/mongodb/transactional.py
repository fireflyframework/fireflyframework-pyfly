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
"""MongoDB and the unified ``@transactional`` decorator.

Use the backend-neutral ``@transactional`` from :mod:`pyfly.data` on document services: it runs the call
in a unit of work of the :class:`~pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager`
named by ``datasource=``, the manager of the service's ``self._motor_client``, or the application's
default datasource. ``mongo_transactional`` is kept as a **deprecated** alias of ``@transactional``, and
:func:`run_mongo_transaction` as a deprecated entry point that runs through the same unit of work.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from typing import Any

from pyfly.data.transaction.decorator import transactional
from pyfly.data.transaction.definition import TransactionDefinition
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.template import TransactionBoundary


async def run_mongo_transaction(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    rollback_for: tuple[type[BaseException], ...] = (Exception,),
    no_rollback_for: tuple[type[BaseException], ...] = (),
) -> Any:
    """Deprecated: run *func* in a MongoDB unit of work (use ``@transactional``).

    The client is ``self._motor_client`` (``args[0]``); the call joins a unit already bound for that client's
    datasource (``Propagation.REQUIRED``) and gets the unit's session as its ``session`` keyword argument.
    *rollback_for* and *no_rollback_for* are additive rules, as on ``@transactional``.
    """
    warnings.warn(
        "run_mongo_transaction is deprecated: decorate the method with @transactional", DeprecationWarning, stacklevel=2
    )
    from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager

    holder = args[0] if args else None
    client = getattr(holder, "_motor_client", None)
    if client is None:
        raise IllegalTransactionStateError(
            f"{getattr(func, '__qualname__', func)}: cannot resolve the Mongo client (pymongo AsyncMongoClient). "
            "Ensure the service has a '_motor_client' attribute."
        )
    manager = MongoTransactionManager.for_client(client)
    definition = TransactionDefinition(rollback_for=tuple(rollback_for), no_rollback_for=tuple(no_rollback_for))
    async with TransactionBoundary(manager, definition) as unit:
        if unit is not None:
            kwargs = {**kwargs, "session": unit.resource}
        return await func(*args, **kwargs)


# Backward-compatibility alias. Prefer the unified `@transactional` from `pyfly.data`.
mongo_transactional = transactional
"""Deprecated alias of :func:`pyfly.data.transaction.decorator.transactional`. Use ``@transactional``."""
