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
"""Session store protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    """Abstract session persistence interface.

    All session backends (in-memory, Redis, etc.) must implement this protocol.
    """

    async def get(self, session_id: str) -> dict[str, Any] | None: ...

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def exists(self, session_id: str) -> bool: ...


@runtime_checkable
class ConditionalSessionStore(SessionStore, Protocol):
    """A :class:`SessionStore` that can write over a session, or move it to a new id, only while it holds it
    (the shipped stores: the in-memory, Redis and SQL ones).

    The ``SessionFilter`` saves a session the store already holds through :meth:`replace`, and moves one whose
    id was rotated outside a login through :meth:`rename`, so a request that ends after its session was logged
    out, evicted or expired does not bring it back, under its id or a new one. A store without them gets every
    change through ``save``, an insert-or-replace, and cannot tell a revoked session from a live one. A
    subclass of a shipped store that overrides ``save`` (to encrypt the data, say) must override
    :meth:`replace` and :meth:`rename` the same way: the filter writes every change of a stored session
    through them.
    """

    async def replace(self, session_id: str, data: dict[str, Any], ttl: int) -> bool:
        """Replace the session's data and expire it *ttl* seconds from now, only if the store holds it and it
        has not expired, in one atomic step; ``False`` (and nothing written) otherwise."""
        ...

    async def rename(self, old_id: str, new_id: str, data: dict[str, Any], ttl: int) -> bool:
        """Move the session from *old_id* to *new_id* with *data*, expiring *ttl* seconds from now, only if the
        store holds it under *old_id* and it has not expired, in one atomic step (*old_id* no longer resolves
        afterwards); ``False`` (and nothing written) otherwise."""
        ...
