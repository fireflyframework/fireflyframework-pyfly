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
"""Integration tests for the Redis DistributedLock + SessionRegistry adapters (v26.06.62).

Exercise the adapters against a REAL Redis (testcontainers) so SET NX PX, the owner-token
release Lua, TTL expiry, and the sorted-set index are validated against real semantics — not
just fakes. Gated by ``@requires_docker``; run in CI (``--all-extras`` + Docker).
"""

from __future__ import annotations

import asyncio

import pytest

from pyfly.testing import requires_docker  # the `redis_url` fixture is provided by conftest.py


@requires_docker
@pytest.mark.asyncio
async def test_redis_lock_against_real_redis(redis_url: str) -> None:
    import redis.asyncio as aioredis

    from pyfly.scheduling.adapters.redis_lock import RedisDistributedLock

    client = aioredis.from_url(redis_url)
    try:
        a, b = RedisDistributedLock(client), RedisDistributedLock(client)  # distinct owner tokens
        assert await a.try_acquire("job", 30.0) is True
        assert await b.try_acquire("job", 30.0) is False  # real SET NX
        await b.release("job")  # non-owner -> owner-token Lua keeps it
        assert await b.try_acquire("job", 30.0) is False
        await a.release("job")  # owner releases
        assert await b.try_acquire("job", 30.0) is True

        # TTL expiry (PX): a short-lived lock frees itself.
        assert await a.try_acquire("ttl", 0.1) is True
        assert await b.try_acquire("ttl", 30.0) is False
        await asyncio.sleep(0.2)
        assert await b.try_acquire("ttl", 30.0) is True  # expired -> re-acquirable
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_session_registry_against_real_redis(redis_url: str) -> None:
    import redis.asyncio as aioredis

    from pyfly.session.adapters.redis_registry import RedisSessionRegistry

    client = aioredis.from_url(redis_url)
    try:
        registry = RedisSessionRegistry(client)
        await registry.register("alice", "s2", 2.0)
        await registry.register("alice", "s1", 1.0)  # older score, registered second
        assert await registry.count("alice") == 2
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["s1", "s2"]  # real ZRANGE oldest-first
        await registry.deregister("alice", "s1")
        assert await registry.count("alice") == 1
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["s2"]
    finally:
        await client.aclose()
