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
"""The Redis OAuth2 token store against a real Redis (WP10b: C016, C072, C073, C074).

The store did a plain GET, then SET or DELETE: one refresh token, code or pushed request_uri was redeemed by
every concurrent request, and a rotation could write a revoked family back. Every grant is now one Lua
script, atomic on the server. The SQL store's matrix is ``test_oauth2_token_store_matrix.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from pyfly.kernel.exceptions import SecurityException
from pyfly.security.adapters.redis_token_store import RedisTokenStore
from pyfly.security.oauth2.authorization_server import AUTHORIZATION_CODE, REFRESH_TOKEN
from pyfly.testing import requires_docker  # `redis_url` fixture provided by conftest.py
from tests.integration import _oauth2_grants as grants


@pytest.fixture
async def redis_store(redis_url: str) -> AsyncIterator[tuple[RedisTokenStore, Any]]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url)
    prefix = f"wp10b:{uuid.uuid4().hex[:8]}:"
    try:
        yield RedisTokenStore(client, key_prefix=prefix), client
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


@requires_docker
async def test_one_refresh_token_is_redeemed_once(redis_store: tuple[RedisTokenStore, Any]) -> None:
    await grants.one_refresh_token_is_redeemed_once(redis_store[0])


@requires_docker
async def test_one_authorization_code_is_redeemed_once(redis_store: tuple[RedisTokenStore, Any]) -> None:
    await grants.one_code_is_redeemed_once(redis_store[0])


@requires_docker
async def test_one_pushed_request_uri_is_consumed_once(redis_store: tuple[RedisTokenStore, Any]) -> None:
    await grants.one_pushed_request_is_consumed_once(redis_store[0])


@requires_docker
@pytest.mark.parametrize("scenario", grants.SEQUENTIAL_SCENARIOS, ids=lambda scenario: scenario.__name__)
async def test_grant_semantics(redis_store: tuple[RedisTokenStore, Any], scenario: Any) -> None:
    await scenario(redis_store[0])


@requires_docker
async def test_rotations_racing_revocations_never_resurrect_a_family(redis_store: tuple[RedisTokenStore, Any]) -> None:
    """C016 on Redis: the thief rotates R1 while the victim replays R0, 30 times. Every family ends revoked."""
    store = redis_store[0]
    authorization_server = grants.server(store)
    for _trial in range(30):
        r0 = await grants.issue(authorization_server)
        r1 = (await grants.refresh(authorization_server, r0))["refresh_token"]

        stolen, _victim = await asyncio.gather(
            grants.attempt(grants.refresh(authorization_server, r1)),
            grants.attempt(grants.refresh(authorization_server, r0)),
        )

        if isinstance(stolen, dict):
            assert not await grants.is_active(authorization_server, stolen["refresh_token"])
        else:
            assert isinstance(stolen, SecurityException)


@requires_docker
async def test_every_key_expires_with_its_record(redis_store: tuple[RedisTokenStore, Any]) -> None:
    """C072: nothing the store writes is kept forever. Every key expires at its record's expiry plus the
    reuse-detection grace period."""
    store, client = redis_store
    authorization_server = grants.server(store, refresh_token_ttl=600)
    token = await grants.issue(authorization_server)
    await grants.refresh(authorization_server, token)
    await grants.code_for(authorization_server)
    await authorization_server.pushed_authorization_request("web", {"scope": "read"})

    keys = [key async for key in client.scan_iter(match=f"{store.key_prefix}*")]
    assert keys
    grace = int(store.purge_grace.total_seconds())
    for key in keys:
        ttl = await client.ttl(key)
        assert 0 < ttl <= 600 + grace + 5, (key, ttl)


@requires_docker
async def test_records_load_by_kind_and_exact_id(redis_store: tuple[RedisTokenStore, Any]) -> None:
    store = redis_store[0]
    authorization_server = grants.server(store)
    token = await grants.issue(authorization_server)

    record = await store.load(REFRESH_TOKEN, token)
    assert record is not None and record.client_id == "svc" and not record.used and record.family_active
    assert await store.load(AUTHORIZATION_CODE, token) is None
    assert await store.load(REFRESH_TOKEN, token.swapcase()) is None
