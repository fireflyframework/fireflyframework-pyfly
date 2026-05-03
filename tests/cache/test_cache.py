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
"""Tests for cache abstraction, in-memory cache, @cache decorator, and CacheManager."""

from datetime import timedelta

import pytest

from pyfly.cache.adapters import InMemoryCache
from pyfly.cache.decorators import cache
from pyfly.cache.manager import CacheManager
from pyfly.cache.ports.outbound import CacheAdapter


class TestInMemoryCache:
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.get("key1") == "value1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self):
        c = InMemoryCache()
        assert await c.get("missing") is None

    @pytest.mark.asyncio
    async def test_evict(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.evict("key1") is True
        assert await c.get("key1") is None

    @pytest.mark.asyncio
    async def test_evict_missing_returns_false(self):
        c = InMemoryCache()
        assert await c.evict("missing") is False

    @pytest.mark.asyncio
    async def test_clear(self):
        c = InMemoryCache()
        await c.put("a", 1)
        await c.put("b", 2)
        await c.clear()
        assert await c.get("a") is None
        assert await c.get("b") is None

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        import asyncio

        c = InMemoryCache()
        await c.put("key", "value", ttl=timedelta(milliseconds=50))
        assert await c.get("key") == "value"
        await asyncio.sleep(0.1)
        assert await c.get("key") is None

    @pytest.mark.asyncio
    async def test_exists_returns_true_for_stored_key(self):
        c = InMemoryCache()
        await c.put("key1", "value1")
        assert await c.exists("key1") is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_missing_key(self):
        c = InMemoryCache()
        assert await c.exists("no-such-key") is False

    @pytest.mark.asyncio
    async def test_exists_returns_false_for_expired_key(self):
        c = InMemoryCache()
        await c.put("key1", "value1", ttl=timedelta(seconds=0))
        assert await c.exists("key1") is False

    @pytest.mark.asyncio
    async def test_protocol_compliance(self):
        """InMemoryCache satisfies the CacheAdapter protocol."""
        c: CacheAdapter = InMemoryCache()
        await c.put("x", 42)
        assert await c.get("x") == 42


class TestCacheDecorator:
    @pytest.mark.asyncio
    async def test_caches_result(self):
        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": "Alice"}

        result1 = await get_user("123")
        result2 = await get_user("123")
        assert result1 == result2
        assert call_count == 1  # Second call served from cache

    @pytest.mark.asyncio
    async def test_different_keys_not_shared(self):
        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        await get_user("1")
        await get_user("2")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cache_with_ttl(self):
        import asyncio

        backend = InMemoryCache()
        call_count = 0

        @cache(backend=backend, key="data:{k}", ttl=timedelta(milliseconds=50))
        async def get_data(k: str) -> str:
            nonlocal call_count
            call_count += 1
            return f"result-{k}"

        await get_data("x")
        assert call_count == 1
        await asyncio.sleep(0.1)
        await get_data("x")
        assert call_count == 2  # TTL expired, re-fetched


class TestCacheManager:
    @pytest.mark.asyncio
    async def test_uses_primary(self):
        primary = InMemoryCache()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback)

        await manager.put("key", "value")
        assert await manager.get("key") == "value"
        # Also written to fallback
        assert await fallback.get("key") == "value"

    @pytest.mark.asyncio
    async def test_failover_to_fallback(self):
        class FailingCache:
            async def get(self, key: str):
                raise ConnectionError("Redis down")

            async def put(self, key: str, value, ttl=None):
                raise ConnectionError("Redis down")

            async def evict(self, key: str) -> bool:
                raise ConnectionError("Redis down")

            async def clear(self) -> None:
                raise ConnectionError("Redis down")

        fallback = InMemoryCache()
        await fallback.put("key", "cached-value")
        manager = CacheManager(primary=FailingCache(), fallback=fallback)

        # Should fall back to InMemoryCache
        result = await manager.get("key")
        assert result == "cached-value"

    @pytest.mark.asyncio
    async def test_evict_from_both(self):
        primary = InMemoryCache()
        fallback = InMemoryCache()
        manager = CacheManager(primary=primary, fallback=fallback)

        await manager.put("key", "value")
        await manager.evict("key")
        assert await primary.get("key") is None
        assert await fallback.get("key") is None
