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
"""Integration tests for the RedisCacheAdapter against a real Redis instance (v26.06.x).

Exercises the distinctive real-Redis paths: PING on start, JSON round-trip,
SET NX (put_if_absent), SCAN-based prefix eviction, EX TTL expiry, and FLUSHDB.
Gated by ``@requires_docker``; run in CI (``--all-extras`` + Docker).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from pyfly.testing import requires_docker  # the `redis_url` fixture is provided by conftest.py


@requires_docker
@pytest.mark.asyncio
async def test_redis_cache_adapter_against_real_redis(redis_url: str) -> None:
    import redis.asyncio as aioredis

    from pyfly.cache.adapters.redis import RedisCacheAdapter

    client = aioredis.from_url(redis_url)
    try:
        cache = RedisCacheAdapter(client)
        await cache.start()  # real PING

        await cache.put("k", {"a": 1})
        assert await cache.get("k") == {"a": 1}  # real round-trip + serialization

        assert await cache.put_if_absent("k", "other") is False  # SET NX on existing key
        assert await cache.put_if_absent("fresh", "v") is True  # SET NX on new key

        await cache.put("p:1", 1)
        await cache.put("p:2", 2)
        assert await cache.evict_by_prefix("p:") == 2  # SCAN-based prefix evict
        assert sorted(await cache.get_keys("p:*")) == []  # SCAN confirms gone

        await cache.put("ttl", "v", ttl=timedelta(seconds=1))  # real EX expiry
        assert await cache.get("ttl") == "v"
        await asyncio.sleep(1.3)
        assert await cache.get("ttl") is None  # expired

        await cache.clear()  # FLUSHDB
        assert await cache.get("fresh") is None
    finally:
        await client.aclose()
