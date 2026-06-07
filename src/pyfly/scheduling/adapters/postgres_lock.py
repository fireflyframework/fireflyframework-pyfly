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
"""Postgres advisory-lock :class:`~pyfly.scheduling.lock.DistributedLock` adapter.

Cluster-safe ``@scheduled`` coordination with **no extra infrastructure** for apps already on
Postgres — uses ``pg_try_advisory_lock`` / ``pg_advisory_unlock``. Hexagonal: the SQLAlchemy
``AsyncEngine`` is injected (lazily, via a factory) by the composition root; this module imports
no SQLAlchemy at module scope.

Session-level advisory locks are tied to the holding connection, so the connection acquired in
``try_acquire`` is **held** until ``release`` (or until the process dies — Postgres then drops
the connection and auto-releases the lock, which is the crash-safety mechanism in lieu of a TTL).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any


class PostgresAdvisoryLock:
    """Distributed lock backed by Postgres session-level advisory locks."""

    def __init__(self, engine_factory: Callable[[], Any]) -> None:
        # A zero-arg callable returning a SQLAlchemy AsyncEngine — resolved lazily (and once)
        # so the lock works regardless of bean-registration order.
        self._engine_factory = engine_factory
        self._engine: Any = None
        self._held: dict[str, Any] = {}  # name -> held AsyncConnection
        self._guard = asyncio.Lock()

    @staticmethod
    def _key(name: str) -> int:
        """Map a lock name to a stable signed 64-bit advisory-lock key (deterministic across
        processes — uses blake2b, not the salted built-in hash)."""
        digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=True)

    def _engine_or_resolve(self) -> Any:
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine

    async def try_acquire(self, name: str, ttl: float) -> bool:
        from sqlalchemy import text

        key = self._key(name)
        conn = await self._engine_or_resolve().connect()
        try:
            result = await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            acquired = bool(result.scalar())
        except BaseException:
            await conn.close()
            raise
        if not acquired:
            await conn.close()  # don't leak the connection when the lock is held elsewhere
            return False
        async with self._guard:
            self._held[name] = conn  # keep the connection — the lock lives with it
        return True

    async def release(self, name: str) -> None:
        from sqlalchemy import text

        async with self._guard:
            conn = self._held.pop(name, None)
        if conn is None:
            return
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key(name)})
        finally:
            await conn.close()
