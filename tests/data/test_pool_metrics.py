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

import uuid
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import REGISTRY  # noqa: E402
from sqlalchemy import text  # noqa: E402

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
