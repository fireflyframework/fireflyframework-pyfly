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
"""The Redis session registry's cap check against a real Redis (WP10b: C155).

The controller listed a principal's sessions and registered the new one in separate calls, so concurrent
logins all got in. The Redis registry checks the cap, evicts and registers in one Lua script.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyfly.session.adapters.redis_registry import RedisSessionRegistry
from pyfly.session.concurrency import AtomicSessionRegistry, ConcurrencyControlPolicy, SessionConcurrencyController
from pyfly.testing import requires_docker

CONCURRENCY = 20


@pytest.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Any]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url)
    try:
        yield client
    finally:
        await client.aclose()


def _registries(client: Any) -> list[RedisSessionRegistry]:
    prefix = f"wp10b:{uuid.uuid4().hex[:8]}:user:"
    return [RedisSessionRegistry(client, key_prefix=prefix), RedisSessionRegistry(client, key_prefix=prefix)]


@requires_docker
@pytest.mark.parametrize("strategy", ["reject-new", "evict-oldest"])
async def test_concurrent_logins_keep_the_cap(redis_client: Any, strategy: str) -> None:
    registries = _registries(redis_client)
    assert isinstance(registries[0], AtomicSessionRegistry)
    evicted: list[str] = []

    async def _delete(session_id: str) -> None:
        evicted.append(session_id)

    controllers = [
        SessionConcurrencyController(
            registry, ConcurrencyControlPolicy(max_sessions=1, strategy=strategy), session_deleter=_delete
        )
        for registry in registries
    ]

    results = await asyncio.gather(
        *(controllers[index % 2].on_login("alice", f"s{index}", float(index)) for index in range(CONCURRENCY))
    )

    assert await registries[0].count("alice") == 1
    if strategy == "reject-new":
        assert results.count(True) == 1
    else:
        assert all(results)
        assert len(evicted) == CONCURRENCY - 1


@requires_docker
async def test_register_limited_evicts_the_oldest(redis_client: Any) -> None:
    registry = _registries(redis_client)[0]
    await registry.register("bob", "s1", 1.0)
    await registry.register("bob", "s2", 2.0)

    rejected = await registry.register_limited("bob", "s3", 3.0, max_sessions=2, evict_oldest=False)
    accepted = await registry.register_limited("bob", "s3", 3.0, max_sessions=2, evict_oldest=True)
    again = await registry.register_limited("bob", "s3", 3.0, max_sessions=2, evict_oldest=False)

    assert not rejected.accepted and rejected.evicted == ()
    assert accepted.accepted and accepted.evicted == ("s1",)
    assert again.accepted and again.evicted == ()
    assert [sid for sid, _ in await registry.list_sessions("bob")] == ["s2", "s3"]
    assert await redis_client.ttl(f"{registry.key_prefix}bob") > 0
