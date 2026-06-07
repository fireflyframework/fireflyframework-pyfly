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
"""Integration tests: persistent OAuth2 token stores against real Redis + Postgres (v26.06.69)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pyfly.testing import postgres_container, pyfly_config_for, redis_container, requires_docker


@pytest.fixture
def redis_url() -> Iterator[str]:
    with redis_container() as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture
def pg_url() -> Iterator[str]:
    with postgres_container() as container:
        yield pyfly_config_for(container)["pyfly.data.relational.url"]


@requires_docker
@pytest.mark.asyncio
async def test_redis_token_store_against_real_redis(redis_url: str) -> None:
    import redis.asyncio as aioredis

    from pyfly.security.adapters.redis_token_store import RedisTokenStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisTokenStore(client, ttl=60)
        await store.store("tok", {"sub": "bob", "scope": "write"})
        assert await store.find("tok") == {"sub": "bob", "scope": "write"}
        await store.revoke("tok")
        assert await store.find("tok") is None
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_postgres_token_store_against_real_postgres(pg_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from pyfly.security.adapters.postgres_token_store import PostgresTokenStore

    engine = create_async_engine(pg_url)
    try:
        store = PostgresTokenStore(lambda: engine)
        await store.store("tok", {"sub": "carol"})
        assert await store.find("tok") == {"sub": "carol"}
        await store.store("tok", {"sub": "carol", "rotated": True})  # upsert
        assert await store.find("tok") == {"sub": "carol", "rotated": True}
        await store.revoke("tok")
        assert await store.find("tok") is None
    finally:
        await engine.dispose()
