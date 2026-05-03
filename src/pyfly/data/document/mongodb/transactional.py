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
"""MongoDB transactional decorator — wraps async functions in a Mongo session + transaction."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def mongo_transactional(func: F) -> F:
    """Wrap an async function in a MongoDB session and transaction.

    The decorated function receives an extra ``session`` keyword argument
    bound to the active ``ClientSession``.  If the function completes
    without error the transaction is committed; otherwise it is aborted.

    Requires a :class:`motor.motor_asyncio.AsyncIOMotorClient` to be
    resolvable from the first positional argument's ``_motor_client``
    attribute (typically ``self`` in a service bean).

    Example::

        class OrderService:
            def __init__(self, motor_client):
                self._motor_client = motor_client

            @mongo_transactional
            async def place_order(self, order, *, session=None):
                ...
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # Resolve the Motor client from the first arg (self)
        self_arg = args[0] if args else None
        motor_client = getattr(self_arg, "_motor_client", None)
        if motor_client is None:
            raise RuntimeError(
                f"{func.__qualname__}: cannot resolve Motor client. Ensure the service has a '_motor_client' attribute."
            )

        async with await motor_client.start_session() as session, session.start_transaction():
            kwargs["session"] = session
            return await func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
