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
"""Integration tests for the PostgresCacheAdapter against a real PostgreSQL instance.

Exercises the distinctive Postgres paths: DDL table creation on start(), JSON round-trip
over BYTEA, INSERT ON CONFLICT upsert, INSERT ON CONFLICT DO NOTHING (put_if_absent),
LIKE-based prefix eviction, TIMESTAMPTZ expiry, and DELETE FROM (clear).
Gated by ``@requires_docker``; run in CI (``--all-extras`` + Docker).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from pyfly.testing import requires_docker  # the `pg_url` fixture is provided by conftest.py


@requires_docker
@pytest.mark.asyncio
async def test_postgres_cache_adapter_against_real_postgres(pg_url: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found,unused-ignore]

    from pyfly.cache.adapters.postgres import PostgresCacheAdapter

    engine = create_async_engine(pg_url)
    cache = PostgresCacheAdapter(engine=engine)
    try:
        await cache.start()  # creates pyfly_cache_entries table (idempotent)

        # ----------------------------------------------------------------
        # Basic put/get round-trip (scalar + dict)
        # ----------------------------------------------------------------
        await cache.put("k", {"a": 1})
        assert await cache.get("k") == {"a": 1}  # BYTEA round-trip + JSON deserialization

        await cache.put("scalar", 42)
        assert await cache.get("scalar") == 42

        # ----------------------------------------------------------------
        # Upsert: put() overwrites an existing key
        # ----------------------------------------------------------------
        await cache.put("k", {"a": 2})
        assert await cache.get("k") == {"a": 2}

        # ----------------------------------------------------------------
        # put_if_absent: True on new key, False on existing
        # ----------------------------------------------------------------
        assert await cache.put_if_absent("k", "other") is False  # DO NOTHING on existing key
        assert await cache.get("k") == {"a": 2}  # original value preserved
        assert await cache.put_if_absent("fresh", "v") is True  # new key inserted
        assert await cache.get("fresh") == "v"

        # ----------------------------------------------------------------
        # evict_by_prefix + get_keys confirms gone
        # ----------------------------------------------------------------
        await cache.put("p:1", 1)
        await cache.put("p:2", 2)
        count = await cache.evict_by_prefix("p:")
        assert count == 2
        assert sorted(await cache.get_keys("p:*")) == []  # LIKE confirms gone

        # ----------------------------------------------------------------
        # TTL expiry (real TIMESTAMPTZ comparison on Postgres)
        # ----------------------------------------------------------------
        await cache.put("ttl_key", "alive", ttl=timedelta(seconds=1))
        assert await cache.get("ttl_key") == "alive"
        await asyncio.sleep(1.3)
        assert await cache.get("ttl_key") is None  # expired

        # ----------------------------------------------------------------
        # exists() honours expiry
        # ----------------------------------------------------------------
        await cache.put("live", "yes")
        assert await cache.exists("live") is True
        assert await cache.exists("no-such") is False

        # ----------------------------------------------------------------
        # evict() returns bool
        # ----------------------------------------------------------------
        await cache.put("del_me", "x")
        assert await cache.evict("del_me") is True
        assert await cache.evict("del_me") is False  # already gone

        # ----------------------------------------------------------------
        # get_stats() reflects activity
        # ----------------------------------------------------------------
        await cache.get("fresh")  # +1 hit
        await cache.get("missing")  # +1 miss
        stats = await cache.get_stats()
        assert stats["type"] == "postgres"
        assert stats["hits"] >= 1
        assert stats["misses"] >= 1

        # ----------------------------------------------------------------
        # clear() removes all entries
        # ----------------------------------------------------------------
        await cache.clear()
        assert await cache.get("fresh") is None
        assert await cache.get("k") is None

    finally:
        await cache.stop()
        await engine.dispose()
