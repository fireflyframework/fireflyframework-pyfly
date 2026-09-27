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
"""Distributed lock for ``@scheduled`` jobs (ShedLock / Spring ``@SchedulerLock`` parity).

When a scheduled job declares ``lock="name"``, the scheduler acquires the lock before each
run and skips the tick if it is held elsewhere — so in a cluster only one instance runs the
job at a time. The default :class:`LocalLock` always acquires (single-instance behavior is
unchanged); register a :class:`DistributedLock` bean to coordinate across instances:

- ``pyfly.scheduling.lock.provider=database`` (or ``postgres``): the portable lease table
  (:class:`~pyfly.scheduling.adapters.lease_lock.LeaseLock`) on the application's datasource;
- ``redis``: ``SET NX PX`` on Redis;
- ``postgres`` with ``pyfly.scheduling.lock.postgres.advisory=true``: PostgreSQL advisory locks, an
  opt-in accelerator (:class:`~pyfly.scheduling.adapters.postgres_lock.PostgresAdvisoryLock`).

Every adapter honors the TTL: a lock taken for *ttl* seconds ends by then even if its holder hangs, so
a job that hangs blocks its schedule for at most ``lock_ttl``.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DistributedLock(Protocol):
    """A best-effort, TTL-bounded named lock.

    An implementation must end the lock after *ttl* seconds when its holder has not released it (a hung
    job), and a release by a holder whose lock already ended must not end a lock another holder took since.
    The lease table tells holders apart per acquisition, the in-process lock per task, and
    ``RedisDistributedLock`` per lock instance (so across nodes, not between two runs in one process).
    """

    async def try_acquire(self, name: str, ttl: float) -> bool:
        """Attempt to acquire *name* for up to *ttl* seconds. Returns whether acquired."""
        ...

    async def release(self, name: str) -> None:
        """Release *name* (no-op if not held)."""
        ...


class LocalLock:
    """No-op lock that always acquires — single-instance default (no coordination)."""

    async def try_acquire(self, name: str, ttl: float) -> bool:
        return True

    async def release(self, name: str) -> None:
        return None


class InProcessDistributedLock:
    """Real mutual exclusion **within one process** (not cross-process) with TTL self-heal.

    Prevents a slow job tick from overlapping its next tick in the same process; for true
    multi-instance coordination use the Redis adapter or the lease table. A held name auto-frees
    after its TTL so a crashed/never-released lock recovers.

    A holder is the task that acquired: once its lock ended at the TTL and another task took the
    name, that task's late release is a no-op (the other holder keeps the lock). A release from any
    other task ends the lock, as before.
    """

    def __init__(self) -> None:
        self._held: dict[str, tuple[float, asyncio.Task[Any] | None]] = {}  # name -> (monotonic expiry, holder)
        # name -> the tasks whose lock on it ended and was taken by another task since
        self._displaced: dict[str, weakref.WeakSet[asyncio.Task[Any]]] = {}
        self._guard = asyncio.Lock()

    async def try_acquire(self, name: str, ttl: float) -> bool:
        async with self._guard:
            now = time.monotonic()
            held = self._held.get(name)
            if held is not None and held[0] > now:
                return False
            task = asyncio.current_task()
            displaced = self._displaced.get(name)
            if held is not None and held[1] is not None and held[1] is not task:
                displaced = self._displaced.setdefault(name, weakref.WeakSet())
                displaced.add(held[1])
            if displaced is not None:
                if task is not None:
                    displaced.discard(task)
                if not displaced:
                    del self._displaced[name]
            self._held[name] = (now + ttl, task)
            return True

    async def release(self, name: str) -> None:
        async with self._guard:
            displaced = self._displaced.get(name)
            task = asyncio.current_task()
            if displaced is not None and task is not None and task in displaced:
                displaced.discard(task)  # its lock ended at the TTL: the name is another holder's now
                if not displaced:
                    del self._displaced[name]
                return
            self._held.pop(name, None)
