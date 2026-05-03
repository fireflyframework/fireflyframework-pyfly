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
"""Tests for @cacheable, @cache_evict, and @cache_put decorators."""

from datetime import timedelta

import pytest

from pyfly.cache.adapters import InMemoryCache
from pyfly.cache.decorators import cache_evict, cache_put, cacheable


class TestCacheable:
    @pytest.mark.asyncio
    async def test_caches_return_value(self):
        backend = InMemoryCache()
        call_count = 0

        @cacheable(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": "Alice"}

        result1 = await get_user("123")
        result2 = await get_user("123")
        assert result1 == result2 == {"id": "123", "name": "Alice"}
        assert call_count == 1  # Second call served from cache

    @pytest.mark.asyncio
    async def test_different_keys_not_shared(self):
        backend = InMemoryCache()
        call_count = 0

        @cacheable(backend=backend, key="user:{user_id}")
        async def get_user(user_id: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        await get_user("1")
        await get_user("2")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cacheable_with_ttl(self):
        backend = InMemoryCache()

        @cacheable(backend=backend, key="item:{item_id}", ttl=timedelta(seconds=60))
        async def get_item(item_id: str) -> str:
            return f"item-{item_id}"

        await get_item("abc")
        stored = await backend.get("item:abc")
        assert stored == "item-abc"


class TestCacheEvict:
    @pytest.mark.asyncio
    async def test_evicts_key_after_method(self):
        backend = InMemoryCache()
        await backend.put("user:42", {"id": "42", "name": "Alice"})

        @cache_evict(backend=backend, key="user:{user_id}")
        async def delete_user(user_id: str) -> None:
            pass  # business logic

        assert await backend.get("user:42") is not None
        await delete_user("42")
        assert await backend.get("user:42") is None

    @pytest.mark.asyncio
    async def test_evict_all_clears_cache(self):
        backend = InMemoryCache()
        await backend.put("user:1", {"id": "1"})
        await backend.put("user:2", {"id": "2"})
        await backend.put("session:abc", "token")

        @cache_evict(backend=backend, all_entries=True)
        async def clear_all() -> str:
            return "cleared"

        result = await clear_all()
        assert result == "cleared"
        assert await backend.get("user:1") is None
        assert await backend.get("user:2") is None
        assert await backend.get("session:abc") is None


class TestCachePut:
    @pytest.mark.asyncio
    async def test_always_executes_and_caches(self):
        backend = InMemoryCache()
        call_count = 0

        @cache_put(backend=backend, key="user:{user_id}")
        async def update_user(user_id: str, name: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id, "name": name}

        result1 = await update_user("1", "Alice")
        assert call_count == 1
        assert result1 == {"id": "1", "name": "Alice"}
        assert await backend.get("user:1") == {"id": "1", "name": "Alice"}

        # Second call always executes (unlike @cacheable)
        result2 = await update_user("1", "Bob")
        assert call_count == 2
        assert result2 == {"id": "1", "name": "Bob"}
        # Cache updated with latest value
        assert await backend.get("user:1") == {"id": "1", "name": "Bob"}
