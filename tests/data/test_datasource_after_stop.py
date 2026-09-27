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
"""After the datasource registry closes, nothing opens a connection through it again.

``AsyncEngine.dispose()`` replaces the pool with an empty one, and the next use of the engine opened
connections in it that nobody would dispose: a late write, a readiness probe that arrived during or
after ``ctx.stop()``, a bean that kept the engine. The closed registry's engines now refuse to
connect, and the db health indicator of a closed registry answers ``OUT_OF_SERVICE`` without
touching its engines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: E402

from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import DataSourceConfigurationError, DataSourceRegistry  # noqa: E402
from pyfly.data.relational.health import SqlAlchemyHealthIndicator  # noqa: E402


def _config(tmp_path: Path) -> Config:
    return Config(
        {
            "pyfly": {
                "data": {
                    "relational": {
                        "enabled": "true",
                        "url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
                        "ddl-auto": "none",
                        "datasources": {"reporting": {"url": f"sqlite+aiosqlite:///{tmp_path / 'reporting.db'}"}},
                    }
                }
            }
        }
    )


async def test_every_engine_of_a_closed_registry_refuses_to_connect(tmp_path: Path) -> None:
    registry = DataSourceRegistry(_config(tmp_path))
    engines = [datasource.engine for datasource in registry.all_datasources()]
    for engine in engines:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    await registry.close()

    for engine in engines:
        with pytest.raises(DataSourceConfigurationError, match="closed"):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        assert engine.pool.checkedin() == 0


async def test_a_readiness_probe_after_stop_answers_out_of_service_without_connecting(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path))
    await context.start()
    indicator = context.get_bean(SqlAlchemyHealthIndicator)
    engine = context.get_bean(AsyncEngine)
    assert (await indicator.health()).status == "UP"
    connects: list[object] = []
    event.listen(engine.sync_engine, "connect", lambda *_: connects.append(object()))

    await context.stop()
    status = await indicator.health()

    assert status.status == "OUT_OF_SERVICE"
    assert connects == []
    assert engine.pool.checkedin() == 0
