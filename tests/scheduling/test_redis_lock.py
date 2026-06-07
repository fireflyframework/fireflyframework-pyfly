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
"""Redis + in-process DistributedLock adapters (v26.06.59)."""

from __future__ import annotations

from typing import Any

import pytest

from pyfly.core.config import Config
from pyfly.scheduling.adapters.redis_lock import RedisDistributedLock
from pyfly.scheduling.lock import DistributedLock, InProcessDistributedLock, LocalLock


class _FakeRedis:
    """Minimal async fake: SET NX + the release-Lua compare-and-del (no time elapses in tests)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None) -> Any:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def eval(self, script: str, numkeys: int, key: str, arg: str) -> int:
        if self.store.get(key) == arg:  # owner-token compare-and-del
            del self.store[key]
            return 1
        return 0


@pytest.mark.asyncio
async def test_redis_lock_acquire_release_cycle() -> None:
    lock = RedisDistributedLock(_FakeRedis())
    assert await lock.try_acquire("job", 30.0) is True
    assert await lock.try_acquire("job", 30.0) is False  # held (SET NX)
    await lock.release("job")
    assert await lock.try_acquire("job", 30.0) is True


@pytest.mark.asyncio
async def test_redis_lock_only_owner_can_release() -> None:
    redis = _FakeRedis()
    a, b = RedisDistributedLock(redis), RedisDistributedLock(redis)  # distinct owner tokens
    assert await a.try_acquire("job", 30.0) is True
    assert await b.try_acquire("job", 30.0) is False
    await b.release("job")  # b doesn't own it -> no-op
    assert await b.try_acquire("job", 30.0) is False  # still a's
    await a.release("job")  # owner releases
    assert await b.try_acquire("job", 30.0) is True


@pytest.mark.asyncio
async def test_redis_lock_satisfies_protocol() -> None:
    assert isinstance(RedisDistributedLock(_FakeRedis()), DistributedLock)


@pytest.mark.asyncio
async def test_inprocess_lock_mutual_exclusion() -> None:
    lock = InProcessDistributedLock()
    assert isinstance(lock, DistributedLock)
    assert await lock.try_acquire("j", 30.0) is True
    assert await lock.try_acquire("j", 30.0) is False
    await lock.release("j")
    assert await lock.try_acquire("j", 30.0) is True


def test_lock_provider_selection() -> None:
    from pyfly.container.container import Container
    from pyfly.scheduling.auto_configuration import SchedulingAutoConfiguration

    ac = SchedulingAutoConfiguration()
    container = Container()
    assert isinstance(ac.distributed_lock(Config({}), container), LocalLock)  # default: none
    memory_cfg = Config({"pyfly": {"scheduling": {"lock": {"provider": "memory"}}}})
    assert isinstance(ac.distributed_lock(memory_cfg, container), InProcessDistributedLock)
