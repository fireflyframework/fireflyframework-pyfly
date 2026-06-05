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
"""Regression tests for cache audit fixes (#72, #75, #76, #78, #80)."""

from __future__ import annotations

import datetime
import decimal

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.health import CacheHealthIndicator
from pyfly.cache.serialization import cache_dumps, cache_loads


def test_serializer_tolerates_framework_types() -> None:
    # audit #72 — must not raise on datetime/Decimal/set.
    value = {"at": datetime.datetime(2026, 6, 4, 12, 0), "amt": decimal.Decimal("1.50"), "tags": {"a", "b"}}
    restored = cache_loads(cache_dumps(value))
    assert restored["amt"] == "1.50"
    assert sorted(restored["tags"]) == ["a", "b"]


@pytest.mark.asyncio
async def test_put_if_absent() -> None:
    cache = InMemoryCache()
    assert await cache.put_if_absent("k", "first") is True
    assert await cache.put_if_absent("k", "second") is False  # audit #75
    assert await cache.get("k") == "first"


@pytest.mark.asyncio
async def test_evict_by_prefix() -> None:
    cache = InMemoryCache()
    await cache.put("user:1", "a")
    await cache.put("user:2", "b")
    await cache.put("order:1", "c")
    removed = await cache.evict_by_prefix("user:")  # audit #78
    assert removed == 2
    assert await cache.get("user:1") is None
    assert await cache.get("order:1") == "c"


@pytest.mark.asyncio
async def test_stats_track_hit_rate() -> None:
    cache = InMemoryCache()
    await cache.put("k", "v")
    await cache.get("k")  # hit
    await cache.get("missing")  # miss
    stats = cache.get_stats()  # audit #76
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.5


@pytest.mark.asyncio
async def test_null_caching_via_decorator() -> None:
    from pyfly.cache.decorators import cache

    backend = InMemoryCache()
    calls = {"n": 0}

    @cache(backend=backend, key="k")
    async def fetch() -> None:
        calls["n"] += 1
        return None

    await fetch()
    await fetch()
    assert calls["n"] == 1  # second call served the cached None (audit #80)


@pytest.mark.asyncio
async def test_cache_health_indicator_up() -> None:
    indicator = CacheHealthIndicator(adapter=InMemoryCache())
    result = await indicator.health()  # audit #74
    assert result.status == "UP"
