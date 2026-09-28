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
"""Pool metrics per datasource (F9): every engine of the registry is observable, not only the primary.

Real SQLite file engines and the real Prometheus registry: the gauges are read at scrape time, so they
match the pool exactly while connections are checked out and after they are returned.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
import weakref
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import REGISTRY  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: E402

from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.auto_configuration import QueryMetricsLifecycle  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceRegistry  # noqa: E402
from pyfly.data.relational.metrics import SqlAlchemyPoolMetrics  # noqa: E402
from pyfly.observability.metrics import MetricsRegistry  # noqa: E402


def _sample(name: str, datasource: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"datasource": datasource})


async def test_pool_gauges_follow_the_pool(tmp_path: Path) -> None:
    name = f"pool-{uuid.uuid4().hex[:8]}"
    registry = DataSourceRegistry(Config({"pyfly": {"data": {"relational": {"pool": {"size": 2, "max-overflow": 1}}}}}))
    try:
        datasource = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
        SqlAlchemyPoolMetrics(MetricsRegistry()).bind(name, datasource.engine)
        assert _sample("pyfly_db_pool_size", name) == 2
        assert _sample("pyfly_db_pool_checked_out", name) == 0

        first = await datasource.engine.connect()
        second = await datasource.engine.connect()
        third = await datasource.engine.connect()
        assert _sample("pyfly_db_pool_checked_out", name) == 3
        assert _sample("pyfly_db_pool_overflow", name) == 1
        for conn in (first, second, third):
            await conn.close()
        assert _sample("pyfly_db_pool_checked_out", name) == 0
        assert _sample("pyfly_db_pool_idle", name) == 2
    finally:
        await registry.close()


async def test_the_time_to_obtain_a_connection_is_measured(tmp_path: Path) -> None:
    name = f"pool-{uuid.uuid4().hex[:8]}"
    registry = DataSourceRegistry(
        Config({"pyfly": {"data": {"relational": {"pool": {"size": 1, "max-overflow": 0, "timeout": 5}}}}})
    )
    try:
        datasource = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
        SqlAlchemyPoolMetrics(MetricsRegistry()).bind(name, datasource.engine)
        held = await datasource.engine.connect()
        assert _sample("pyfly_db_pool_acquire_seconds_count", name) == 1

        async def _return_it_later() -> None:
            await asyncio.sleep(0.2)
            await held.close()

        returning = asyncio.ensure_future(_return_it_later())
        async with datasource.engine.connect() as conn:  # waits for the only connection
            await conn.execute(text("SELECT 1"))
        await returning
        assert _sample("pyfly_db_pool_acquire_seconds_count", name) == 2
        assert (_sample("pyfly_db_pool_acquire_seconds_sum", name) or 0.0) >= 0.18
        assert _acquire_bucket(name, 0.1) == 1  # the first checkout did not wait

        await datasource.engine.dispose()  # a replaced pool keeps reporting
        async with datasource.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        assert _sample("pyfly_db_pool_acquire_seconds_count", name) == 3
    finally:
        await registry.close()


def _acquire_bucket(datasource: str, le: float) -> float | None:
    return REGISTRY.get_sample_value("pyfly_db_pool_acquire_seconds_bucket", {"datasource": datasource, "le": str(le)})


async def test_invalidated_connections_are_counted(tmp_path: Path) -> None:
    name = f"pool-{uuid.uuid4().hex[:8]}"
    registry = DataSourceRegistry(Config({}))
    try:
        datasource = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
        SqlAlchemyPoolMetrics(MetricsRegistry()).bind(name, datasource.engine)
        before = _sample("pyfly_db_pool_invalidated_total", name) or 0.0
        async with datasource.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.invalidate()
        assert _sample("pyfly_db_pool_invalidated_total", name) == before + 1
    finally:
        await registry.close()


async def test_lifecycle_covers_datasources_registered_later(tmp_path: Path) -> None:
    config = Config({"pyfly": {"data": {"relational": {"url": f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"}}}})
    registry = DataSourceRegistry.for_config(config)
    try:
        lifecycle = QueryMetricsLifecycle(registry.primary.engine, MetricsRegistry(), datasource_registry=registry)
        await lifecycle.start()
        name = f"late-{uuid.uuid4().hex[:8]}"
        late = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'late.db'}")
        async with late.engine.connect() as conn:
            assert _sample("pyfly_db_pool_checked_out", name) == 1
            await conn.execute(text("SELECT 1"))
        assert _sample("pyfly_db_pool_checked_out", name) == 0
    finally:
        await registry.close()


async def test_context_exports_every_datasource(tmp_path: Path) -> None:
    reporting = f"reporting{uuid.uuid4().hex[:6]}"
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": "true",
                            "ddl-auto": "none",
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
                            "datasources": {reporting: {"url": f"sqlite+aiosqlite:///{tmp_path / 'r.db'}"}},
                        }
                    },
                }
            }
        )
    )
    await context.start()  # the metrics auto-configuration provides the MetricsRegistry
    try:
        registry = context.get_bean(DataSourceRegistry)
        async with registry.get(reporting).engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            assert _sample("pyfly_db_pool_checked_out", reporting) == 1
    finally:
        await context.stop()


async def test_exported_pools_do_not_keep_a_dropped_registry_alive(tmp_path: Path) -> None:
    # The Prometheus registry is process-wide; its gauges read the pool at scrape time and must not pin
    # the engine they read once its registry is closed and dropped (a restarted context builds anew).
    name = f"dropped-{uuid.uuid4().hex[:8]}"
    metrics = SqlAlchemyPoolMetrics(MetricsRegistry())
    registry = DataSourceRegistry(Config({}))
    datasource = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    metrics.bind(name, datasource.engine)
    await _select_one(datasource.engine)
    assert _sample("pyfly_db_pool_idle", name) == 1
    engine = weakref.ref(datasource.engine.sync_engine)  # what a scrape-time reading would hold
    await registry.close()
    del registry, datasource
    gc.collect()

    assert engine() is None
    assert _sample("pyfly_db_pool_idle", name) == 0  # the scrape still works, and reads an empty pool

    # A new engine under the same label is exported, even should it reuse the dropped engine's id.
    registry = DataSourceRegistry(Config({}))
    try:
        replacement = registry.register(name, f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
        metrics.bind(name, replacement.engine)
        async with replacement.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            assert _sample("pyfly_db_pool_checked_out", name) == 1
    finally:
        await registry.close()


async def _select_one(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
