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

Through the whole login request (``SessionFilter`` plus ``OAuth2LoginHandler``, on the Redis session store), a
session a concurrent login evicted came back when its own login's request ended, and stayed live uncounted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyfly.container.container import Container
from pyfly.session.adapters.redis import RedisSessionStore
from pyfly.session.adapters.redis_registry import RedisSessionRegistry
from pyfly.session.concurrency import AtomicSessionRegistry, ConcurrencyControlPolicy, SessionConcurrencyController
from pyfly.testing import requires_docker
from tests.integration import _session_logins as logins

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
@pytest.mark.parametrize("strategy", ["evict-oldest", "reject-new"])
@pytest.mark.parametrize(("max_sessions", "concurrent"), [(1, 8), (2, 16)])
async def test_concurrent_logins_through_the_login_flow_keep_the_cap(
    redis_client: Any, redis_url: str, strategy: str, max_sessions: int, concurrent: int
) -> None:
    """Rounds of concurrent OAuth2 logins of one principal through two instances, each with its own client of
    the Redis session store and registry: after each round only the registered sessions are in the store."""
    import redis.asyncio as aioredis

    other_client = aioredis.from_url(redis_url)
    principal = f"alice-{uuid.uuid4().hex[:8]}"
    registries = [_registries(redis_client)[0]]
    registries.append(RedisSessionRegistry(other_client, key_prefix=registries[0].key_prefix))
    try:
        flows = []
        for client, registry in zip((redis_client, other_client), registries, strict=True):
            store = RedisSessionStore(client)
            policy = ConcurrencyControlPolicy(max_sessions=max_sessions, strategy=strategy)
            controller = SessionConcurrencyController(registry, policy, session_store=store)
            flows.append(logins.Replica(store, controller, principal=principal))

        await logins.concurrent_logins_keep_the_cap(
            flows,
            max_sessions=max_sessions,
            evict_oldest=strategy == "evict-oldest",
            concurrent=concurrent,
            rounds=3,
        )
    finally:
        await redis_client.delete(f"{registries[0].key_prefix}{principal}")
        await other_client.aclose()


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


@requires_docker
async def test_the_redis_registry_beside_process_local_stores_caps_every_instance(
    redis_client: Any, redis_url: str
) -> None:
    """C154 (review): two instances share the Redis registry, each keeps its sessions in memory. Asking its own
    store about the other instance's live session, the second instance dropped its registration and let a
    second login in: the auto-configuration must not hand a process-local store to the controller."""
    from pyfly.core.config import Config
    from pyfly.session.adapters.memory import InMemorySessionStore
    from pyfly.session.auto_configuration import SessionConcurrencyAutoConfiguration

    config = Config(
        {
            "pyfly": {
                "session": {
                    "enabled": True,
                    "concurrency": {
                        "enabled": True,
                        "registry": "redis",
                        "max-sessions": 1,
                        "strategy": "reject-new",
                        "redis": {"url": redis_url},
                    },
                }
            }
        }
    )
    stores = [InMemorySessionStore(), InMemorySessionStore()]
    controllers = [
        SessionConcurrencyAutoConfiguration().session_concurrency_controller(config, store, Container())
        for store in stores
    ]
    principal = f"carol-{uuid.uuid4().hex[:8]}"
    try:
        await stores[0].save("on-first", {"user": principal}, ttl=600)
        assert await controllers[0].on_login(principal, "on-first", 1.0)
        await stores[1].save("on-second", {"user": principal}, ttl=600)

        assert not await controllers[1].on_login(principal, "on-second", 2.0)
        assert [sid for sid, _ in await controllers[1].registry.list_sessions(principal)] == ["on-first"]
    finally:
        await redis_client.delete(f"{RedisSessionRegistry(redis_client).key_prefix}{principal}")
        for controller in controllers:
            await controller.registry._client.aclose()  # type: ignore[attr-defined]
