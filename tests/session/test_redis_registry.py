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
"""Redis SessionRegistry adapter (v26.06.59)."""

from __future__ import annotations

import pytest

from pyfly.session.adapters.redis_registry import RedisSessionRegistry
from pyfly.session.concurrency import SessionRegistry


class _FakeRedis:
    """Minimal async fake of the sorted-set ops the registry uses; members returned as bytes."""

    def __init__(self) -> None:
        self.z: dict[str, dict[str, float]] = {}

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.z.setdefault(key, {}).update(mapping)

    async def zrem(self, key: str, member: str) -> None:
        self.z.get(key, {}).pop(member, None)

    async def zrange(self, key: str, start: int, stop: int, withscores: bool = False) -> list:
        items = sorted(self.z.get(key, {}).items(), key=lambda kv: kv[1])
        return [(m.encode(), s) for m, s in items] if withscores else [m.encode() for m, s in items]

    async def zcard(self, key: str) -> int:
        return len(self.z.get(key, {}))

    async def expire(self, key: str, ttl: int) -> None:
        return None


@pytest.mark.asyncio
async def test_registry_is_oldest_first_and_counts() -> None:
    reg = RedisSessionRegistry(_FakeRedis())
    assert isinstance(reg, SessionRegistry)
    await reg.register("alice", "s2", 2.0)
    await reg.register("alice", "s1", 1.0)  # registered second but older score
    assert await reg.count("alice") == 2
    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s1", "s2"]  # oldest-first, decoded


@pytest.mark.asyncio
async def test_registry_deregister() -> None:
    reg = RedisSessionRegistry(_FakeRedis())
    await reg.register("bob", "s1", 1.0)
    await reg.register("bob", "s2", 2.0)
    await reg.deregister("bob", "s1")
    assert [sid for sid, _ in await reg.list_sessions("bob")] == ["s2"]
    assert await reg.count("bob") == 1
