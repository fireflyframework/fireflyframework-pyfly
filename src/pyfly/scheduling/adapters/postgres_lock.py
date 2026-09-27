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
- an acquisition belongs to the task that took it. Once the watchdog ended a task's lock (or the server ended
  the session that held it), that task is *displaced*: its late release is a no-op, so it never ends the lock
  a later run of the job in this process took since. A release from any other task ends the lock;
- an acquisition or an unlock that fails, whatever the error (a cancellation included), discards the
  connection instead of returning it to the pool: the pool's reset (a ``ROLLBACK``) keeps a session lock,
  which would stay taken for good.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import weakref
from collections.abc import Callable, Coroutine
from typing import Any

_logger = logging.getLogger(__name__)


class _Hold:
    """A held lock: its connection, the task that took it, and the watchdog that ends it at the TTL."""

    __slots__ = ("connection", "task", "watchdog")

    def __init__(self, connection: Any, task: asyncio.Task[Any] | None) -> None:
        self.connection = connection
        self.task = task
        self.watchdog: asyncio.TimerHandle | None = None


class PostgresAdvisoryLock:
    """Distributed lock backed by Postgres session-level advisory locks (see the module documentation).

    *engine_factory* is the ``AsyncEngine`` (or a ``DataSource``), or a zero-argument callable returning it,
    resolved once, at the first acquisition.
    """

    def __init__(self, engine_factory: Callable[[], Any] | Any) -> None:
        self._engine_factory = engine_factory
        self._engine: Any = None
        # The bookkeeping below changes only between awaits, so the event loop keeps it consistent.
        self._held: dict[str, _Hold] = {}
        # name -> the tasks whose lock on it ended without their release: a late release of theirs is a no-op
        self._displaced: dict[str, weakref.WeakSet[asyncio.Task[Any]]] = {}
        self._ending: set[asyncio.Task[None]] = set()

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
        """Release every lock still held and wait for the watchdogs' unlocks (the jobs have drained by now).
        Idempotent."""
        while self._held:
            name, hold = self._held.popitem()
            self._cancel_watchdog(hold)
            await self._end(name, hold)
        self._displaced.clear()
        if self._ending:
            await asyncio.gather(*self._ending, return_exceptions=True)

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
            # The server may have granted the lock before the error (or the cancellation) reached us: discard
            # the session, never return it to the pool, whose ROLLBACK would keep the lock for good.
            await self._discard(conn)
            raise
        if not acquired:
            await conn.close()  # don't leak the connection when the lock is held elsewhere
            return False
        lost = self._held.pop(name, None)
        if lost is not None:
            # The server granted the lock, so the session of the hold still recorded for it ended (a restart,
            # a terminated backend): that holder lost its lock, and its late release must not end this one.
            _logger.warning(
                "scheduler_advisory_lock_lost",
                extra={"lock": name, "hint": "the session holding the lock ended while its job ran"},
            )
            self._retire(name, lost, self._discard(lost.connection))
        task = asyncio.current_task()
        hold = _Hold(conn, task)
        self._held[name] = hold  # keep the connection — the lock lives with it
        displaced = self._displaced.get(name)
        if displaced is not None:
            if task is not None:
                displaced.discard(task)  # it holds the lock again
            if not displaced:
                del self._displaced[name]
        hold.watchdog = asyncio.get_running_loop().call_later(max(ttl, 0.0), self._expire, name, hold)
        return True

    async def release(self, name: str) -> None:
        """Unlock *name* and return its connection; on any unlock failure the connection is discarded (the
        server ends the session and its locks) and the failure is raised.

        A no-op when *name* is not held, and when the running task is displaced (its lock ended at the TTL):
        whoever holds *name* since keeps it. A release from any other task ends the lock."""
        task = asyncio.current_task()
        hold = self._held.get(name)
        if hold is None or hold.task is not task:
            displaced = self._displaced.get(name)
            if displaced is not None and task is not None and task in displaced:
                displaced.discard(task)
                if not displaced:
                    del self._displaced[name]
                return
        if hold is None:
            return
        del self._held[name]
        self._cancel_watchdog(hold)
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
            await self._discard(conn)
            raise
        await conn.close()

    async def _end(self, name: str, hold: _Hold) -> None:
        """Unlock *hold* where nobody awaits the failure: it is logged (the connection was discarded, and the
        server ended the session and its lock)."""
        try:
            await self._unlock(name, hold)
        except Exception:  # noqa: BLE001 — logged; the connection is gone
            _logger.warning("scheduler_advisory_unlock_failed", extra={"lock": name}, exc_info=True)

    @staticmethod
    async def _discard(conn: Any) -> None:
        """Invalidate *conn* (the server ends its session, and the session's locks) and give its pool slot
        back."""
        with contextlib.suppress(Exception):
            await conn.invalidate()
        with contextlib.suppress(Exception):
            await conn.close()

    @staticmethod
    def _cancel_watchdog(hold: _Hold) -> None:
        if hold.watchdog is not None:
            hold.watchdog.cancel()

    def _retire(self, name: str, hold: _Hold, ending: Coroutine[Any, Any, None]) -> None:
        """Displace the task of *hold* (no longer in ``_held``) and run *ending*, which frees its connection, in
        the background; :meth:`stop` waits for it."""
        self._cancel_watchdog(hold)
        if hold.task is not None:
            self._displaced.setdefault(name, weakref.WeakSet()).add(hold.task)
        task = asyncio.get_running_loop().create_task(ending)
        self._ending.add(task)
        task.add_done_callback(self._ending.discard)

    def _expire(self, name: str, hold: _Hold) -> None:
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
        self._retire(name, hold, self._end(name, hold))
