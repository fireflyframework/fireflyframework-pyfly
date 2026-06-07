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
"""Read/write datasource routing — the Spring ``AbstractRoutingDataSource`` equivalent.

A :class:`RoutingSessionFactory` picks the primary or the read-replica session maker
based on a context "lookup key": whether the current block is marked read-only via
:func:`read_only`. Routing is opt-in — with no replica configured, the factory always
uses the primary (current behavior).

Usage::

    factory = ctx.get_bean(RoutingSessionFactory)

    async def list_users() -> list[User]:
        with read_only():                 # routes to the replica when one is configured
            session = factory()
            ...
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_read_only: ContextVar[bool] = ContextVar("pyfly_db_read_only", default=False)


def is_read_only() -> bool:
    """Whether the current context is marked read-only (routes to the replica)."""
    return _read_only.get()


@contextlib.contextmanager
def read_only() -> Iterator[None]:
    """Mark the enclosed block read-only so the routing factory uses the replica.

    Nesting is supported (the prior value is restored on exit). The Spring
    ``@Transactional(readOnly = true)`` analogue.
    """
    token = _read_only.set(True)
    try:
        yield
    finally:
        _read_only.reset(token)


class RoutingSessionFactory:
    """Calls the replica session maker inside a :func:`read_only` block (when a replica
    is configured), otherwise the primary. Drop-in replacement for an
    ``async_sessionmaker`` call site: ``factory()`` returns an ``AsyncSession``.
    """

    def __init__(
        self,
        primary: Callable[[], AsyncSession],
        replica: Callable[[], AsyncSession] | None = None,
    ) -> None:
        self._primary = primary
        self._replica = replica

    @property
    def has_replica(self) -> bool:
        return self._replica is not None

    def primary(self) -> AsyncSession:
        """Force a primary (read/write) session regardless of context."""
        return self._primary()

    def replica(self) -> AsyncSession:
        """Force a replica session, falling back to primary when none is configured."""
        return self._replica() if self._replica is not None else self._primary()

    def __call__(self) -> Any:
        """Route by context: replica when read-only and configured, else primary."""
        if self._replica is not None and is_read_only():
            return self._replica()
        return self._primary()
