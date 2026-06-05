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
"""Regression tests for cache hardening (v26.06.15).

- CacheManager satisfies the full CacheAdapter protocol and works as a decorator
  backend (it used to lack exists/put_if_absent/evict_by_prefix/start/stop).
- Cache decorators reject a sync target with a clear error at decoration time.
- A bad key template raises a clear ValueError (not a cryptic KeyError).
- InMemoryCache(max_size=...) bounds the cache with LRU eviction; default is unbounded.
"""

from __future__ import annotations

import pytest

from pyfly.cache import CacheAdapter, CacheManager, cacheable
from pyfly.cache.adapters.memory import InMemoryCache


class TestCacheManagerProtocol:
    def test_satisfies_cache_adapter_protocol(self) -> None:
        mgr = CacheManager(InMemoryCache(), InMemoryCache())
        assert isinstance(mgr, CacheAdapter)

    @pytest.mark.asyncio
    async def test_new_methods_delegate_to_both(self) -> None:
        mgr = CacheManager(InMemoryCache(), InMemoryCache())
        await mgr.start()
        assert await mgr.put_if_absent("k", "v") is True
        assert await mgr.put_if_absent("k", "v2") is False  # already present
        assert await mgr.exists("k") is True

        await mgr.put("p:1", 1)
        await mgr.put("p:2", 2)
        assert await mgr.evict_by_prefix("p:") >= 2
        assert await mgr.exists("p:1") is False
        await mgr.stop()

    @pytest.mark.asyncio
    async def test_usable_as_decorator_backend(self) -> None:
        # The cacheable decorator calls backend.exists() on the null-caching path;
        # a CacheManager used to AttributeError there. Now it works.
        mgr = CacheManager(InMemoryCache(), InMemoryCache())
        calls = {"n": 0}

        @cacheable(backend=mgr, key="x:{n}")
        async def get(n: int) -> None:
            calls["n"] += 1
            return None

        assert await get(1) is None
        assert await get(1) is None  # cached None -> exists() path, no AttributeError
        assert calls["n"] == 1


class TestDecoratorGuards:
    def test_sync_function_rejected_at_decoration(self) -> None:
        with pytest.raises(TypeError, match="requires an async function"):

            @cacheable(backend=InMemoryCache(), key="k")
            def sync_fn() -> int:
                return 1

    @pytest.mark.asyncio
    async def test_bad_key_template_gives_clear_error(self) -> None:
        @cacheable(backend=InMemoryCache(), key="x:{missing}")
        async def fn(n: int) -> int:
            return n

        with pytest.raises(ValueError, match="unknown parameter"):
            await fn(1)


class TestInMemoryMaxSize:
    @pytest.mark.asyncio
    async def test_lru_eviction_when_full(self) -> None:
        cache = InMemoryCache(max_size=2)
        await cache.put("a", 1)
        await cache.put("b", 2)
        assert await cache.get("a") == 1  # 'a' becomes most-recently-used; 'b' is LRU
        await cache.put("c", 3)  # over capacity -> evict LRU ('b')

        assert await cache.exists("b") is False
        assert await cache.get("a") == 1
        assert await cache.get("c") == 3
        assert cache.get_stats()["max_size"] == 2

    @pytest.mark.asyncio
    async def test_unbounded_by_default(self) -> None:
        cache = InMemoryCache()
        for i in range(100):
            await cache.put(f"k{i}", i)
        assert len(cache.get_keys()) == 100
        assert cache.get_stats()["max_size"] is None
