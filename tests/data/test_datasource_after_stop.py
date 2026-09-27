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

A connection that is in use while the registry closes finishes its work, and is closed when it is
returned. ``AsyncEngine.dispose()`` only closes the idle connections: a returned one went back into
the disposed pool and stayed open until the garbage collector found that pool (on PostgreSQL the
backend stayed in ``pg_stat_activity`` after ``ctx.stop()``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational.datasource_registry import (  # noqa: E402
    DataSourceConfigurationError,
    DataSourceRegistry,
    close_connections_on_return,
)
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


def _closed_connections(engine: AsyncEngine) -> list[Any]:
    """The DBAPI connections of *engine*'s pool closed from now on (SQLAlchemy's ``close`` pool event)."""
    closed: list[Any] = []
    event.listen(engine.sync_engine, "close", lambda dbapi_connection, _record: closed.append(dbapi_connection))
    return closed


async def _dbapi_connection(conn: AsyncConnection) -> Any:
    return (await conn.get_raw_connection()).dbapi_connection


async def test_a_connection_in_use_while_the_registry_closes_is_closed_when_returned(tmp_path: Path) -> None:
    registry = DataSourceRegistry(_config(tmp_path))
    engine = registry.primary.engine
    closed = _closed_connections(engine)
    async with engine.connect() as conn:
        in_use = await _dbapi_connection(conn)
        await registry.close()
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1  # it finishes its work
        assert in_use not in closed

    assert in_use in closed  # returned: closed, not kept in the disposed pool


async def test_a_connection_in_use_while_the_context_stops_is_closed_when_returned(tmp_path: Path) -> None:
    """A readiness probe or a request in flight when the last step of ``ctx.stop()`` closes the registry."""
    context = ApplicationContext(_config(tmp_path))
    await context.start()
    engine = context.get_bean(AsyncEngine)
    closed = _closed_connections(engine)
    async with engine.connect() as conn:
        in_use = await _dbapi_connection(conn)
        await context.stop()
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        assert in_use not in closed

    assert in_use in closed


async def test_an_engine_disposed_by_its_owner_closes_the_connections_returned_after(tmp_path: Path) -> None:
    """``close_connections_on_return`` is what an owner calls before disposing an engine of its own. It
    closes the connections in use at the dispose when they are returned, and only those: the engine pools
    again afterwards (it used to pool nothing any more)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'own.db'}")
    closed = _closed_connections(engine)
    connects: list[object] = []
    event.listen(engine.sync_engine, "connect", lambda *_: connects.append(object()))
    async with engine.connect() as conn:
        in_use = await _dbapi_connection(conn)
        close_connections_on_return(engine)
        await engine.dispose()
        await conn.execute(text("SELECT 1"))
        assert in_use not in closed

    assert in_use in closed
    connects.clear()
    later: list[Any] = []
    for _ in range(5):
        async with engine.connect() as conn:
            later.append(await _dbapi_connection(conn))
    assert len(connects) == 1  # one pooled connection served all five
    assert later[0] not in closed
    await engine.dispose()


async def test_a_second_dispose_closes_the_connections_in_use_then_too(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'own.db'}")
    closed = _closed_connections(engine)
    close_connections_on_return(engine)
    await engine.dispose()
    async with engine.connect() as conn:
        in_use = await _dbapi_connection(conn)
        close_connections_on_return(engine)
        await engine.dispose()
        assert in_use not in closed

    assert in_use in closed
    await engine.dispose()


async def test_an_in_memory_database_keeps_its_data_after_the_hook() -> None:
    """On a StaticPool the one connection is the database: closing it on return lost every table."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    close_connections_on_return(engine)
    await engine.dispose()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE wp07_memory (x INTEGER)"))
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM wp07_memory"))).scalar_one() == 0
    await engine.dispose()
