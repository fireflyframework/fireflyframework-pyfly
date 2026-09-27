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
"""Cache adapter protocol."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CacheAdapter(Protocol):
    """Abstract cache interface.

    All cache backends (Redis, in-memory, etc.) must implement this protocol. The contract the built-in
    adapters keep, and a custom one should keep (the PostgreSQL adapter does not keep the own-namespace
    ``clear()``, expiry and named-cache rules yet: its ``clear()`` empties the cache table, it leaves expired
    rows in it, and it has no ``with_namespace``):

    - **Values are copies.** ``put`` stores a copy of the value and ``get`` returns a value of the caller's
      own: changing either never changes the entry, whatever the backend.
    - **No live ORM objects.** ``put`` and ``put_if_absent`` raise
      :class:`~pyfly.cache.serialization.CacheValueError` (a ``TypeError``) for a SQLAlchemy-mapped instance
      or a Beanie document, also inside a container or a DTO, and for a value the backend cannot encode,
      before anything is written.
    - **clear() is this cache's own.** It removes this cache's entries and nothing else: an adapter over a
      store other data may share (a Redis database, a database table) deletes its namespace only and never
      flushes the store.
    - **Expired entries do not accumulate.** An entry past its TTL is never returned, and the store drops it
      even when no one reads it again (Redis expires keys itself, the in-memory cache sweeps them): the
      CQRS query cache writes one entry per caller's scope and relies on TTLs to bound its size.
    - **Named caches (optional).** ``with_namespace(name)`` returns a cache disjoint from this one, whose
      entries this cache's ``clear()`` never touches (see :func:`~pyfly.cache.namespaces.dedicated_cache`).
      Durable consumers (idempotency records, orchestration state) keep their entries there.

    Writes are not transaction-aware by themselves; wrap the adapter in
    :class:`~pyfly.cache.transaction.TransactionAwareCache` (the decorators and the CQRS query cache do) to
    defer them to the commit of the current unit of work.
    """

    async def get(self, key: str) -> Any | None:
        """The value stored for *key* (a copy), or ``None`` when absent or expired."""
        ...

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Store a copy of *value* under *key*, expiring after *ttl* (never when ``None``)."""
        ...

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store *value* only when *key* is absent, atomically; whether it was stored."""
        ...

    async def evict(self, key: str) -> bool:
        """Remove *key*; whether it existed."""
        ...

    async def evict_by_prefix(self, prefix: str) -> int:
        """Remove every key of this cache starting with *prefix* (taken literally); how many were removed."""
        ...

    async def exists(self, key: str) -> bool:
        """Whether *key* is present and not expired (a stored ``None`` counts)."""
        ...

    async def clear(self) -> None:
        """Remove every entry of this cache, and nothing else in its store."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
