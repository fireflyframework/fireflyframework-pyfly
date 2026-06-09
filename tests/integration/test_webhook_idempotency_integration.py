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
"""Integration tests for :class:`~pyfly.webhooks.redis_event_store.RedisWebhookEventStore`.

Gated by ``@requires_docker``; run with::

    PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \\
        tests/integration/test_webhook_idempotency_integration.py -q
"""

from __future__ import annotations

import pytest

from pyfly.testing import requires_docker


@requires_docker
@pytest.mark.asyncio
async def test_redis_store_basic_idempotency(redis_url: str) -> None:
    """First call returns False, remember stores, second call returns True."""
    import redis.asyncio as aioredis

    from pyfly.webhooks.redis_event_store import RedisWebhookEventStore

    client = aioredis.from_url(redis_url)
    try:
        store = RedisWebhookEventStore(client, ttl_seconds=60)
        key = "test-basic-idem-key-001"

        assert await store.already_processed(key) is False
        await store.remember(key)
        assert await store.already_processed(key) is True
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_store_distributed_visibility(redis_url: str) -> None:
    """A second store instance connecting to the same Redis sees the stored key."""
    import redis.asyncio as aioredis

    from pyfly.webhooks.redis_event_store import RedisWebhookEventStore

    client_a = aioredis.from_url(redis_url)
    client_b = aioredis.from_url(redis_url)
    try:
        store_a = RedisWebhookEventStore(client_a, ttl_seconds=60)
        store_b = RedisWebhookEventStore(client_b, ttl_seconds=60)
        key = "test-distributed-idem-key-002"

        # Before: neither instance sees the key
        assert await store_a.already_processed(key) is False
        assert await store_b.already_processed(key) is False

        # Instance A remembers the key
        await store_a.remember(key)

        # Instance B (separate client connection) must also see it
        assert await store_b.already_processed(key) is True
    finally:
        await client_a.aclose()
        await client_b.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_store_ttl_is_set(redis_url: str) -> None:
    """The key must have a positive TTL (expiry) set after remember()."""
    import redis.asyncio as aioredis

    from pyfly.webhooks.redis_event_store import RedisWebhookEventStore

    client = aioredis.from_url(redis_url)
    try:
        ttl_seconds = 120
        store = RedisWebhookEventStore(client, ttl_seconds=ttl_seconds)
        key = "test-ttl-key-003"

        await store.remember(key)
        full_key = store._prefix + key  # noqa: SLF001  — white-box TTL assertion
        ttl: int = await client.ttl(full_key)
        # TTL should be positive and ≤ configured value (Redis rounds up to seconds)
        assert 0 < ttl <= ttl_seconds
    finally:
        await client.aclose()
