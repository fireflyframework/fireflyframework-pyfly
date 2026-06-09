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
"""Unit tests for PostgresCacheAdapter.

These tests run against an in-memory SQLite engine (via aiosqlite) so no Docker
is required.  The SQL used by the adapter is deliberately kept portable
(LIKE/LIMIT/INSERT ON CONFLICT DO NOTHING|UPDATE all work on SQLite ≥ 3.24).

BYTEA vs BLOB: SQLite stores ``bytes`` values in a BLOB column regardless of the
DDL type name (``BYTEA``), so the round-trip works identically.
TIMESTAMPTZ: SQLite stores it as TEXT/REAL; comparison with a naive datetime
value via ``>`` works because both sides are ISO-format strings when using the
aiosqlite dialect.  The adapter always stores naive UTC datetimes, so the
comparison is consistent.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pyfly.cache.adapters.postgres import PostgresCacheAdapter, _glob_to_like
from pyfly.cache.ports.outbound import CacheAdapter

# ---------------------------------------------------------------------------
# _glob_to_like helper
# ---------------------------------------------------------------------------


class TestGlobToLike:
    def test_star_becomes_percent(self) -> None:
        assert _glob_to_like("foo*") == "foo%"

    def test_question_mark_becomes_underscore(self) -> None:
        assert _glob_to_like("foo?bar") == "foo_bar"

    def test_literal_percent_is_escaped(self) -> None:
        assert _glob_to_like("100%") == r"100\%"

    def test_literal_underscore_is_escaped(self) -> None:
        assert _glob_to_like("a_b") == r"a\_b"

    def test_wildcard_only(self) -> None:
        assert _glob_to_like("*") == "%"

    def test_mixed(self) -> None:
        assert _glob_to_like("pre:*:suf?") == "pre:%:suf_"


# ---------------------------------------------------------------------------
# Adapter against SQLite in-memory
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache() -> PostgresCacheAdapter:
    """Return a started PostgresCacheAdapter backed by SQLite in-memory."""
    from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found,unused-ignore]

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    adapter = PostgresCacheAdapter(engine=engine)
    await adapter.start()
    return adapter


class TestPostgresCacheAdapterSQLite:
    """Full behaviour tests using an in-memory SQLite engine (no Docker)."""

    @pytest.mark.asyncio
    async def test_protocol_compliance(self, cache: PostgresCacheAdapter) -> None:
        """PostgresCacheAdapter satisfies the CacheAdapter protocol."""
        adapter: CacheAdapter = cache
        await adapter.put("x", 42)
        assert await adapter.get("x") == 42

    @pytest.mark.asyncio
    async def test_put_and_get_scalar(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("num", 123)
        assert await cache.get("num") == 123

    @pytest.mark.asyncio
    async def test_put_and_get_dict(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("obj", {"name": "Alice", "age": 30})
        assert await cache.get("obj") == {"name": "Alice", "age": 30}

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, cache: PostgresCacheAdapter) -> None:
        assert await cache.get("no-such-key") is None

    @pytest.mark.asyncio
    async def test_put_overwrites(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("k", "first")
        await cache.put("k", "second")
        assert await cache.get("k") == "second"

    @pytest.mark.asyncio
    async def test_exists_true(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("e", "v")
        assert await cache.exists("e") is True

    @pytest.mark.asyncio
    async def test_exists_false(self, cache: PostgresCacheAdapter) -> None:
        assert await cache.exists("missing") is False

    @pytest.mark.asyncio
    async def test_evict_returns_true(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("del", "v")
        assert await cache.evict("del") is True
        assert await cache.get("del") is None

    @pytest.mark.asyncio
    async def test_evict_missing_returns_false(self, cache: PostgresCacheAdapter) -> None:
        assert await cache.evict("no-such") is False

    @pytest.mark.asyncio
    async def test_evict_by_prefix(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("p:1", 1)
        await cache.put("p:2", 2)
        await cache.put("q:3", 3)
        count = await cache.evict_by_prefix("p:")
        assert count == 2
        assert await cache.get("p:1") is None
        assert await cache.get("p:2") is None
        assert await cache.get("q:3") == 3

    @pytest.mark.asyncio
    async def test_put_if_absent_returns_true_on_new_key(self, cache: PostgresCacheAdapter) -> None:
        assert await cache.put_if_absent("fresh", "v") is True
        assert await cache.get("fresh") == "v"

    @pytest.mark.asyncio
    async def test_put_if_absent_returns_false_on_existing_key(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("exists", "original")
        assert await cache.put_if_absent("exists", "other") is False
        assert await cache.get("exists") == "original"

    @pytest.mark.asyncio
    async def test_clear_removes_all(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("a", 1)
        await cache.put("b", 2)
        await cache.clear()
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    @pytest.mark.asyncio
    async def test_get_keys_all(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("x:1", 1)
        await cache.put("x:2", 2)
        keys = await cache.get_keys("*")
        assert "x:1" in keys
        assert "x:2" in keys

    @pytest.mark.asyncio
    async def test_get_keys_pattern(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("ns:a", 1)
        await cache.put("ns:b", 2)
        await cache.put("other:c", 3)
        keys = await cache.get_keys("ns:*")
        assert set(keys) == {"ns:a", "ns:b"}

    @pytest.mark.asyncio
    async def test_get_keys_limit(self, cache: PostgresCacheAdapter) -> None:
        for i in range(10):
            await cache.put(f"k{i}", i)
        keys = await cache.get_keys("*", limit=5)
        assert len(keys) <= 5

    @pytest.mark.asyncio
    async def test_get_stats_type(self, cache: PostgresCacheAdapter) -> None:
        stats = await cache.get_stats()
        assert stats["type"] == "postgres"

    @pytest.mark.asyncio
    async def test_get_stats_hit_rate(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("k", "v")
        await cache.get("k")  # hit
        await cache.get("missing")  # miss
        stats = await cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_start_creates_table(self, cache: PostgresCacheAdapter) -> None:
        """start() is idempotent — calling it again should not raise."""
        await cache.start()  # second call; table already exists

    @pytest.mark.asyncio
    async def test_stop_is_noop(self, cache: PostgresCacheAdapter) -> None:
        await cache.stop()  # must not raise


# ---------------------------------------------------------------------------
# Auto-configuration provider selection (no Docker needed)
# ---------------------------------------------------------------------------


class TestPostgresCacheAutoConfiguration:
    """Assert that provider=postgres wires up a PostgresCacheAdapter."""

    def test_cache_adapter_returns_postgres_adapter(self) -> None:
        from pyfly.cache.adapters.postgres import PostgresCacheAdapter
        from pyfly.cache.auto_configuration import CacheAutoConfiguration

        config = MagicMock()
        config.get = MagicMock(
            side_effect=lambda key, default=None: {
                "pyfly.cache.provider": "postgres",
                "pyfly.cache.postgres.url": "postgresql+asyncpg://localhost:5432/cache",
            }.get(key, default)
        )

        fake_engine = MagicMock()
        with (
            patch(
                "pyfly.cache.auto_configuration.AutoConfiguration.is_available",
                return_value=True,
            ),
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                return_value=fake_engine,
            ),
        ):
            ac = CacheAutoConfiguration()
            adapter = ac.cache_adapter(config)

        assert isinstance(adapter, PostgresCacheAdapter)
