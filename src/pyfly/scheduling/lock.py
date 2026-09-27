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
from typing import Protocol, runtime_checkable


@runtime_checkable
class DistributedLock(Protocol):
    """A best-effort, TTL-bounded named lock.

    An implementation must end the lock after *ttl* seconds when its holder has not released it (a hung
    job), and a release by a holder whose lock already ended must not end a lock another holder took since.
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
    multi-instance coordination use the Redis adapter. A held name auto-frees after its TTL so
    a crashed/never-released lock recovers.
    """

    def __init__(self) -> None:
        self._held: dict[str, float] = {}  # name -> monotonic expiry
        self._guard = asyncio.Lock()

    async def try_acquire(self, name: str, ttl: float) -> bool:
        async with self._guard:
            expiry = self._held.get(name)
            if expiry is not None and expiry > time.monotonic():
                return False
            self._held[name] = time.monotonic() + ttl
            return True

    async def release(self, name: str) -> None:
        async with self._guard:
            self._held.pop(name, None)
