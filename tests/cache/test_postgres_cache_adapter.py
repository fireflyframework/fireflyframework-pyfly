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

These tests run against a SQLite file database (via aiosqlite), so no Docker is required: the adapter's
table is a portable Core table and its upserts are the dialect's own. Every server backend (PostgreSQL,
MySQL, MariaDB) runs in ``tests/integration/test_cache_postgres_integration.py``, with the time-zone,
expiry, purge and round-trip checks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

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
        assert _glob_to_like("100%") == "100!%"

    def test_literal_underscore_is_escaped(self) -> None:
        assert _glob_to_like("a_b") == "a!_b"

    def test_the_escape_character_itself_is_escaped(self) -> None:
        # "!" reads the same in every dialect's string literal; a backslash does not (MySQL).
        assert _glob_to_like("wow!*") == "wow!!%"

    def test_wildcard_only(self) -> None:
        assert _glob_to_like("*") == "%"

    def test_mixed(self) -> None:
        assert _glob_to_like("pre:*:suf?") == "pre:%:suf_"


# ---------------------------------------------------------------------------
# Adapter against a SQLite file
# ---------------------------------------------------------------------------


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[PostgresCacheAdapter]:
    """Return a started PostgresCacheAdapter backed by a SQLite file database."""
    from sqlalchemy.ext.asyncio import create_async_engine  # type: ignore[import-not-found,unused-ignore]

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}")
    adapter = PostgresCacheAdapter(engine=engine)
    await adapter.start()
    try:
        yield adapter
    finally:
        await engine.dispose()


class TestPostgresCacheAdapterSQLite:
    """Full behavior tests using a SQLite file database (no Docker)."""

    @pytest.mark.asyncio
    async def test_protocol_compliance(self, cache: PostgresCacheAdapter) -> None:
        """PostgresCacheAdapter satisfies the CacheAdapter protocol."""
        adapter: CacheAdapter = cache
        await adapter.put("x", 42)
        assert await adapter.get("x") == 42

    @pytest.mark.asyncio
    async def test_a_dedicated_cache_name_cannot_hold_the_namespace_separator(
        self, cache: PostgresCacheAdapter
    ) -> None:
        """``with_namespace("a:b")`` would store under ``pyfly:cache.a:b:``, inside what
        ``with_namespace("a").clear()`` deletes: the name is refused, and names that share a start stay apart."""
        for name in ("a:b", ""):
            with pytest.raises(ValueError, match="name"):
                cache.with_namespace(name)
        first, second = cache.with_namespace("orders"), cache.with_namespace("orders.archive")
        await first.put("k", 1)
        await second.put("k", 2)
        await first.clear()
        assert await first.get("k") is None and await second.get("k") == 2

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
    async def test_put_if_absent_takes_an_expired_key(self, cache: PostgresCacheAdapter) -> None:
        await cache.put("lock", "old", ttl=timedelta(microseconds=1))
        assert await cache.put_if_absent("lock", "new") is True
        assert await cache.get("lock") == "new"

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
    """Assert that provider=postgres wires up a PostgresCacheAdapter on the right datasource."""

    async def test_cache_adapter_returns_postgres_adapter(self) -> None:
        # pyfly.cache.postgres.url resolves through the datasource registry: one engine per database.
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = Config(
            {
                "pyfly": {
                    "cache": {"provider": "postgres", "postgres": {"url": "postgresql+asyncpg://localhost:5432/cache"}},
                    "data": {"relational": {"url": "postgresql+asyncpg://localhost:5432/cache"}},
                }
            }
        )
        registry = DataSourceRegistry.for_config(config)
        try:
            with patch("pyfly.cache.auto_configuration.AutoConfiguration.is_available", return_value=True):
                adapter = CacheAutoConfiguration().cache_adapter(config)
            assert isinstance(adapter, PostgresCacheAdapter)
            assert adapter.engine is registry.primary.engine  # the same database: the primary's engine
        finally:
            await registry.close()

    async def test_the_datasource_key_names_the_cache_datasource(self, tmp_path: Path) -> None:
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = Config(
            {
                "pyfly": {
                    "cache": {"provider": "postgres", "postgres": {"datasource": "caching"}},
                    "data": {
                        "relational": {
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                            "datasources": {"caching": {"url": f"sqlite+aiosqlite:///{tmp_path / 'cache.db'}"}},
                        }
                    },
                }
            }
        )
        registry = DataSourceRegistry.for_config(config)
        try:
            adapter = CacheAutoConfiguration().cache_adapter(config)
            assert isinstance(adapter, PostgresCacheAdapter)
            assert adapter.engine is registry.get("caching").engine
        finally:
            await registry.close()

    async def test_the_context_registry_bean_is_the_one_the_cache_runs_on(self, tmp_path: Path) -> None:
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.container.container import Container
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = Config({"pyfly": {"cache": {"provider": "postgres"}}})
        own = DataSourceRegistry(
            Config({"pyfly": {"data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"}}}})
        )
        container = Container()
        container.register_instance(DataSourceRegistry, own)
        try:
            adapter = CacheAutoConfiguration().cache_adapter(config, container)
            assert isinstance(adapter, PostgresCacheAdapter)
            assert adapter.engine is own.primary.engine
        finally:
            await own.close()

    async def test_with_ddl_auto_none_the_cache_only_checks_its_table(self, tmp_path: Path) -> None:
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry
        from pyfly.data.relational.framework_schema import FrameworkSchemaError

        config = Config(
            {
                "pyfly": {
                    "cache": {"provider": "postgres"},
                    "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", "ddl-auto": "none"}},
                }
            }
        )
        registry = DataSourceRegistry.for_config(config)
        try:
            adapter = CacheAutoConfiguration().cache_adapter(config)
            with pytest.raises(FrameworkSchemaError, match="pyfly_cache_entries does not exist"):
                await adapter.start()  # type: ignore[attr-defined]
        finally:
            await registry.close()

    @pytest.mark.parametrize(
        ("configured", "seconds"),
        [(60, 60.0), ("2.5", 2.5), ("90s", 90.0), ("500ms", 0.5), ("2m", 120.0), ("1h", 3600.0), ("0", None)],
    )
    async def test_the_purge_interval_is_a_duration(
        self, tmp_path: Path, configured: object, seconds: float | None
    ) -> None:
        """``purge-interval: 60s`` failed the startup with a bare ``ValueError`` from ``float()``."""
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = Config(
            {
                "pyfly": {
                    "cache": {"provider": "postgres", "postgres": {"purge-interval": configured}},
                    "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"}},
                }
            }
        )
        registry = DataSourceRegistry.for_config(config)
        try:
            adapter = CacheAutoConfiguration().cache_adapter(config)
            assert isinstance(adapter, PostgresCacheAdapter)
            assert adapter._purge_interval == seconds
        finally:
            await registry.close()

    @pytest.mark.parametrize("configured", ["soon", "-5s", "10 minutes"])
    async def test_a_purge_interval_that_is_not_a_duration_names_the_key(self, tmp_path: Path, configured: str) -> None:
        from pyfly.cache.auto_configuration import CacheAutoConfiguration
        from pyfly.core.config import Config
        from pyfly.data.relational.datasource_registry import DataSourceRegistry

        config = Config(
            {
                "pyfly": {
                    "cache": {"provider": "postgres", "postgres": {"purge-interval": configured}},
                    "data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"}},
                }
            }
        )
        registry = DataSourceRegistry.for_config(config)
        try:
            with pytest.raises(ValueError, match=r"pyfly\.cache\.postgres\.purge-interval"):
                CacheAutoConfiguration().cache_adapter(config)
        finally:
            await registry.close()

    def test_the_configured_postgres_url_is_not_a_made_up_default(self) -> None:
        """configprops reported postgres.url=localhost:5432/cache, a database nothing connects to: with no URL
        and no datasource the cache is on the primary datasource."""
        from pyfly.config.properties.cache import CacheProperties

        assert CacheProperties().postgres == {"url": None, "datasource": None, "purge-interval": 60}
