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
"""Tests for RedisCacheAdapter using a FakeRedis stub."""

from __future__ import annotations

from datetime import timedelta

import pytest

from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cache.ports.outbound import CacheAdapter


class FakeRedis:
    """Minimal in-memory stub matching the redis.asyncio.Redis interface."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, *keys: str) -> int:
        count = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                count += 1
        return count

    async def exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self._store)

    async def flushdb(self) -> None:
        self._store.clear()

    async def aclose(self) -> None:
        pass


class TestRedisCacheAdapter:
    @pytest.mark.asyncio
    async def test_protocol_compliance(self):
        """RedisCacheAdapter satisfies the CacheAdapter protocol."""
        adapter: CacheAdapter = RedisCacheAdapter(FakeRedis())
        await adapter.put("x", 42)
        assert await adapter.get("x") == 42

    @pytest.mark.asyncio
    async def test_put_and_get(self):
        adapter = RedisCacheAdapter(FakeRedis())
        await adapter.put("key", {"name": "Alice", "age": 30})
        result = await adapter.get("key")
        assert result == {"name": "Alice", "age": 30}

    @pytest.mark.asyncio
    async def test_get_missing_key(self):
        adapter = RedisCacheAdapter(FakeRedis())
        assert await adapter.get("no-such-key") is None

    @pytest.mark.asyncio
    async def test_evict(self):
        adapter = RedisCacheAdapter(FakeRedis())
        await adapter.put("key", "value")
        assert await adapter.evict("key") is True
        assert await adapter.get("key") is None

    @pytest.mark.asyncio
    async def test_evict_missing_returns_false(self):
        adapter = RedisCacheAdapter(FakeRedis())
        assert await adapter.evict("missing") is False

    @pytest.mark.asyncio
    async def test_exists(self):
        adapter = RedisCacheAdapter(FakeRedis())
        await adapter.put("key", "value")
        assert await adapter.exists("key") is True
        assert await adapter.exists("missing") is False

    @pytest.mark.asyncio
    async def test_clear(self):
        adapter = RedisCacheAdapter(FakeRedis())
        await adapter.put("a", 1)
        await adapter.put("b", 2)
        await adapter.clear()
        assert await adapter.get("a") is None
        assert await adapter.get("b") is None

    @pytest.mark.asyncio
    async def test_put_with_ttl(self):
        """TTL is forwarded to the underlying client as integer seconds."""
        fake = FakeRedis()
        adapter = RedisCacheAdapter(fake)

        calls: list[dict] = []
        original_set = fake.set

        async def tracking_set(key: str, value: bytes, ex: int | None = None) -> None:
            calls.append({"key": key, "value": value, "ex": ex})
            await original_set(key, value, ex=ex)

        fake.set = tracking_set  # type: ignore[assignment]

        await adapter.put("key", "val", ttl=timedelta(seconds=60))
        assert len(calls) == 1
        assert calls[0]["ex"] == 60
        assert await adapter.get("key") == "val"
