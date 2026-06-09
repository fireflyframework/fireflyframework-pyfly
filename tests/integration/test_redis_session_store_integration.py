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
"""Integration tests for :class:`RedisSessionStore` against a real Redis instance.

Validates JSON type-tag serialization + allowlisted reconstruction (the
SecurityContext round-trip), TTL expiry, exists/delete semantics — all against
real Redis semantics via testcontainers. Gated by ``@requires_docker``; run in
CI with ``PYFLY_INTEGRATION_REQUIRE_DOCKER=1 pytest -m integration
tests/integration/test_redis_session_store_integration.py``
"""

from __future__ import annotations

import asyncio

import pytest

from pyfly.testing import requires_docker


@requires_docker
@pytest.mark.asyncio
async def test_session_store_round_trip_with_security_context(redis_url: str) -> None:
    """save + get round-trips a session containing a SecurityContext."""
    import redis.asyncio as aioredis

    from pyfly.security.context import SecurityContext
    from pyfly.session.adapters.redis import RedisSessionStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisSessionStore(client)
        sid = "sess-roundtrip-1"
        ctx = SecurityContext(user_id="u-42", roles=["admin", "user"], permissions=["read:items"])
        data = {"security": ctx, "username": "alice"}

        await store.save(sid, data, ttl=60)
        loaded = await store.get(sid)

        assert loaded is not None
        assert loaded["username"] == "alice"
        # SecurityContext must be reconstructed (not returned as a plain dict)
        sec = loaded["security"]
        assert isinstance(sec, SecurityContext)
        assert sec.user_id == "u-42"
        assert sec.roles == ["admin", "user"]
        assert sec.permissions == ["read:items"]
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_session_store_exists_and_delete(redis_url: str) -> None:
    """exists() reflects save/delete lifecycle."""
    import redis.asyncio as aioredis

    from pyfly.session.adapters.redis import RedisSessionStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisSessionStore(client)
        sid = "sess-exists-1"

        assert await store.exists(sid) is False

        await store.save(sid, {"x": 1}, ttl=60)
        assert await store.exists(sid) is True

        await store.delete(sid)
        assert await store.exists(sid) is False

        # get after delete must return None
        assert await store.get(sid) is None
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_session_store_ttl_expiry(redis_url: str) -> None:
    """A session saved with a short TTL disappears after it expires."""
    import redis.asyncio as aioredis

    from pyfly.session.adapters.redis import RedisSessionStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisSessionStore(client)
        sid = "sess-ttl-expiry"

        await store.save(sid, {"ephemeral": True}, ttl=1)
        assert await store.exists(sid) is True

        # Wait for the TTL to expire (1 s TTL + 0.3 s margin)
        await asyncio.sleep(1.3)

        assert await store.get(sid) is None
        assert await store.exists(sid) is False
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_session_store_get_missing_returns_none(redis_url: str) -> None:
    """get() for a session that was never saved returns None."""
    import redis.asyncio as aioredis

    from pyfly.session.adapters.redis import RedisSessionStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisSessionStore(client)
        result = await store.get("sess-never-saved-xyz")
        assert result is None
    finally:
        await client.aclose()
