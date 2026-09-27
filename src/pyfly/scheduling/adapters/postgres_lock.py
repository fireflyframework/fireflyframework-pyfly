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
"""Postgres advisory-lock :class:`~pyfly.scheduling.lock.DistributedLock` adapter: an opt-in accelerator.

The default database lock is the portable lease table
(:class:`~pyfly.scheduling.adapters.lease_lock.LeaseLock`). This adapter is for PostgreSQL applications that
want a lock the server releases the moment its holder's connection goes away
(``pyfly.scheduling.lock.provider=postgres`` with ``pyfly.scheduling.lock.postgres.advisory=true``). It uses
``pg_try_advisory_lock`` / ``pg_advisory_unlock``; this module imports no SQLAlchemy at module scope.

A session-level advisory lock lives with the connection that took it, so that connection is held until
:meth:`~PostgresAdvisoryLock.release`, and:

- it runs in ``AUTOCOMMIT``: the connection is *idle* while the job runs, never *idle in transaction*, so a
  server's ``idle_in_transaction_session_timeout`` cannot drop it (and the lock) mid-job;
- a watchdog ends the lock at its TTL: a hung job's lock is released (the connection closed) and a WARNING
  logged, so the job runs elsewhere after ``lock_ttl`` instead of never;
- an unlock that fails, whatever the error, discards the connection instead of returning it to the pool:
  the pool's reset (a ``ROLLBACK``) keeps a session lock, which would stay taken for good.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)


class _Hold:
    """A held lock: its connection, and the watchdog that ends it at the TTL."""

    __slots__ = ("connection", "watchdog")

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.watchdog: asyncio.TimerHandle | None = None


class PostgresAdvisoryLock:
    """Distributed lock backed by Postgres session-level advisory locks (see the module documentation).

    *engine_factory* is the ``AsyncEngine`` (or a ``DataSource``), or a zero-argument callable returning it,
    resolved once, at the first acquisition.
    """

    def __init__(self, engine_factory: Callable[[], Any] | Any) -> None:
        self._engine_factory = engine_factory
        self._engine: Any = None
        self._held: dict[str, _Hold] = {}
        self._expiring: set[asyncio.Task[None]] = set()
        self._guard = asyncio.Lock()

    @staticmethod
    def _key(name: str) -> int:
        """Map a lock name to a stable signed 64-bit advisory-lock key (deterministic across
        processes — uses blake2b, not the salted built-in hash)."""
        digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big", signed=True)

    def _engine_or_resolve(self) -> Any:
        if self._engine is None:
            from pyfly.data.relational.framework_schema import framework_engine

            target = self._engine_factory() if callable(self._engine_factory) else self._engine_factory
            self._engine = framework_engine(target).execution_options(isolation_level="AUTOCOMMIT")
        return self._engine

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Nothing to prepare: advisory locks need no table."""

    async def stop(self) -> None:
        """Release every lock still held and stop the watchdogs (the jobs have drained by now). Idempotent."""
        for name in list(self._held):
            with contextlib.suppress(Exception):  # the unlock failure is logged; the connection is gone
                await self.release(name)
        if self._expiring:
            await asyncio.gather(*self._expiring, return_exceptions=True)

    # ------------------------------------------------------------------
    # DistributedLock
    # ------------------------------------------------------------------

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
        hold = _Hold(conn)
        async with self._guard:
            self._held[name] = hold  # keep the connection — the lock lives with it
        hold.watchdog = asyncio.get_running_loop().call_later(max(ttl, 0.0), self._expire, name, hold)
        return True

    async def release(self, name: str) -> None:
        """Unlock *name* and return its connection; on any unlock failure the connection is discarded (the
        server ends the session and its locks) and the failure is raised."""
        async with self._guard:
            hold = self._held.pop(name, None)
        if hold is None:
            return
        if hold.watchdog is not None:
            hold.watchdog.cancel()
        await self._unlock(name, hold)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _unlock(self, name: str, hold: _Hold) -> None:
        from sqlalchemy import text

        conn = hold.connection
        try:
            result = await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key(name)})
            if not result.scalar():
                _logger.warning("scheduler_advisory_lock_not_held", extra={"lock": name})
        except BaseException:
            # Discard, never return: a pooled session keeps the lock through the pool's ROLLBACK.
            with contextlib.suppress(Exception):
                await conn.invalidate()
            raise
        finally:
            await conn.close()

    def _expire(self, name: str, hold: _Hold) -> None:
        task = asyncio.get_running_loop().create_task(self._end_expired(name, hold))
        self._expiring.add(task)
        task.add_done_callback(self._expiring.discard)

    async def _end_expired(self, name: str, hold: _Hold) -> None:
        async with self._guard:
            if self._held.get(name) is not hold:
                return  # released meanwhile
            del self._held[name]
        _logger.warning(
            "scheduler_advisory_lock_expired",
            extra={
                "lock": name,
                "hint": "the job ran longer than its lock ttl: the lock is released so the job can run elsewhere; "
                "raise lock_ttl above the job's longest run",
            },
        )
        try:
            await self._unlock(name, hold)
        except Exception:  # noqa: BLE001 — the connection was discarded; the server released the lock
            _logger.warning("scheduler_advisory_unlock_failed", extra={"lock": name}, exc_info=True)
