# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
"""The shared native cache is an explicit infrastructure dependency when enabled."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.auto_configuration import CacheAutoConfiguration
from pyfly.config.properties.cache import CacheProperties
from pyfly.core.config import Config


def test_default_and_auto_select_postgres_even_with_redis_installed() -> None:
    assert Config._load_framework_defaults()["pyfly"]["cache"]["provider"] == "postgres"
    assert CacheProperties().provider == "postgres"
    with patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=True):
        assert CacheAutoConfiguration.detect_provider() == "postgres"


@pytest.mark.parametrize("provider", ["auto", "postgres"])
def test_missing_sql_support_cannot_select_local_memory(provider: str) -> None:
    with (
        patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=False),
        pytest.raises(ValueError, match="requires SQLAlchemy"),
    ):
        CacheAutoConfiguration().cache_adapter(Config({"pyfly": {"cache": {"provider": provider}}}))


@pytest.mark.parametrize("provider", ["redis", "typo", ""])
def test_unavailable_or_unknown_explicit_provider_fails(provider: str) -> None:
    with (
        patch("pyfly.config.auto.AutoConfiguration.is_available", return_value=False),
        pytest.raises(ValueError, match="provider|requires"),
    ):
        CacheAutoConfiguration().cache_adapter(Config({"pyfly": {"cache": {"provider": provider}}}))


def test_memory_is_an_explicit_choice() -> None:
    assert isinstance(
        CacheAutoConfiguration().cache_adapter(Config({"pyfly": {"cache": {"provider": "memory"}}})), InMemoryCache
    )


async def test_default_borrows_registered_primary_and_cannot_coordinate_on_sqlite(tmp_path: Path) -> None:
    from pyfly.cache import CacheCoordination
    from pyfly.cache.adapters.postgres import PostgresCacheAdapter
    from pyfly.container.container import Container
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    config = Config({"pyfly": {"data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'main.db'}"}}}})
    registry = DataSourceRegistry(config)
    container = Container()
    container.register_instance(DataSourceRegistry, registry)
    try:
        cache = CacheAutoConfiguration().cache_adapter(config, container)
        assert isinstance(cache, PostgresCacheAdapter)
        assert isinstance(cache, CacheCoordination)
        assert cache.engine is registry.primary.engine
        assert cache.with_namespace("rate").engine is registry.primary.engine
        with pytest.raises(ValueError, match="PostgreSQL primary"):
            await cache.consume_window("actor", 1, timedelta(seconds=60))
    finally:
        await registry.close()


async def test_native_context_exposes_coordination_on_the_same_cache_bean(tmp_path: Path) -> None:
    from pyfly.cache import CacheAdapter, CacheCoordination
    from pyfly.cache.adapters.postgres import PostgresCacheAdapter
    from pyfly.context.application_context import ApplicationContext
    from pyfly.data.relational.datasource_registry import DataSourceRegistry

    config = Config(
        {
            "pyfly": {
                "cache": {"enabled": True},
                "data": {"relational": {"enabled": True, "url": f"sqlite+aiosqlite:///{tmp_path / 'context.db'}"}},
            }
        }
    )
    context = ApplicationContext(config)
    await context.start()
    try:
        cache = context.get_bean(CacheAdapter)
        assert context.get_bean(PostgresCacheAdapter) is cache
        assert context.get_bean(CacheCoordination) is cache
        assert cache.engine is context.get_bean(DataSourceRegistry).primary.engine
    finally:
        await context.stop()
