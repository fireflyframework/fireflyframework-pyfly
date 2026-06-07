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
"""@scheduled distributed lock (v26.06.53)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pyfly.scheduling import DistributedLock, LocalLock, scheduled
from pyfly.scheduling.task_scheduler import TaskScheduler


class _AllowLock:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def try_acquire(self, name: str, ttl: float) -> bool:
        return True

    async def release(self, name: str) -> None:
        self.released.append(name)


class _DenyLock:
    def __init__(self) -> None:
        self.released: list[str] = []

    async def try_acquire(self, name: str, ttl: float) -> bool:
        return False

    async def release(self, name: str) -> None:
        self.released.append(name)


@pytest.mark.asyncio
async def test_local_lock_always_acquires() -> None:
    lock = LocalLock()
    assert await lock.try_acquire("x", 1.0) is True
    await lock.release("x")  # no-op
    assert isinstance(lock, DistributedLock)  # runtime_checkable Protocol


@pytest.mark.asyncio
async def test_invoke_skips_when_lock_denied() -> None:
    lock = _DenyLock()
    scheduler = TaskScheduler(lock=lock)
    ran: list[int] = []

    async def job() -> None:
        ran.append(1)

    await scheduler._invoke(None, job, lock="L", lock_ttl=5.0)
    assert ran == []  # tick skipped (held elsewhere)
    assert lock.released == []  # never acquired -> never released


@pytest.mark.asyncio
async def test_invoke_runs_and_releases_when_acquired() -> None:
    lock = _AllowLock()
    scheduler = TaskScheduler(lock=lock)
    ran: list[int] = []

    async def job() -> None:
        ran.append(1)

    await scheduler._invoke(None, job, lock="L", lock_ttl=5.0)
    assert ran == [1]
    assert lock.released == ["L"]


@pytest.mark.asyncio
async def test_invoke_releases_even_on_failure() -> None:
    lock = _AllowLock()
    scheduler = TaskScheduler(lock=lock)

    async def job() -> None:
        raise ValueError("boom")

    await scheduler._invoke(None, job, lock="L")  # logged, not raised
    assert lock.released == ["L"]  # released in finally


def test_decorator_records_lock_metadata() -> None:
    @scheduled(fixed_rate=timedelta(seconds=1), lock=True, lock_ttl=timedelta(seconds=30))
    def job() -> None:
        pass

    assert job.__pyfly_scheduled_lock__ is True  # type: ignore[attr-defined]
    assert job.__pyfly_scheduled_lock_ttl__ == 30.0  # type: ignore[attr-defined]


def test_discover_resolves_lock_name() -> None:
    scheduler = TaskScheduler()

    class Worker:
        @scheduled(fixed_rate=timedelta(seconds=1), lock=True)
        async def tick(self) -> None: ...

        @scheduled(fixed_rate=timedelta(seconds=1), lock="custom")
        async def tock(self) -> None: ...

        @scheduled(fixed_rate=timedelta(seconds=1))
        async def plain(self) -> None: ...

    scheduler.discover([Worker()])
    by_name = {e.method.__name__: e for e in scheduler._entries}
    assert by_name["tick"].lock == "Worker.tick"  # True -> auto-derived name
    assert by_name["tock"].lock == "custom"
    assert by_name["plain"].lock is None
