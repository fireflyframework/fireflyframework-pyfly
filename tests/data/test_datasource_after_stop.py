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

import asyncio
import random
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy")

import aiosqlite  # noqa: E402
from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from pyfly.context.application_context import ApplicationContext  # noqa: E402
from pyfly.core.config import Config  # noqa: E402
from pyfly.data.relational import datasource_registry  # noqa: E402
from pyfly.data.relational.datasource_registry import (  # noqa: E402
    DataSourceConfigurationError,
    DataSourceRegistry,
    _record_pool,
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


# ---------------------------------------------------------------------------
# The hook is a checkin listener installed when the engine is built. Installed at the close, it added pool
# listeners while a connect event could be suspended inside an awaiting listener (asyncpg's codec setup, a
# session-settings listener), and that connect then failed with "deque mutated during iteration". It
# closes a connection returned to a pool the engine no longer uses, which also covers a connection that a
# connect in flight across the dispose opens in the old pool (a generation stamp gave it the new one).
# ---------------------------------------------------------------------------


def _suspend_connects(engine: AsyncEngine) -> asyncio.Event:
    """Make every new connection await inside the ``connect`` event; the event is set once one does."""
    inside = asyncio.Event()

    def _slow_setup(dbapi_connection: Any, _record: Any) -> None:
        inside.set()
        dbapi_connection.await_(asyncio.sleep(0.1))

    event.listen(engine.sync_engine, "connect", _slow_setup)
    return inside


async def _select_one(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT 1"))).scalar_one())


async def test_a_connect_suspended_in_its_connect_event_survives_the_registry_close(tmp_path: Path) -> None:
    registry = DataSourceRegistry(_config(tmp_path))
    engine = registry.primary.engine
    inside = _suspend_connects(engine)
    request = asyncio.create_task(_select_one(engine))
    await inside.wait()  # a readiness probe connecting while the context stops

    await registry.close()

    assert await request == 1


async def test_installing_the_hook_while_a_connect_is_suspended_leaves_that_connect_alone(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'own.db'}")
    inside = _suspend_connects(engine)
    request = asyncio.create_task(_select_one(engine))
    await inside.wait()
    close_connections_on_return(engine)  # late: not when the engine was built
    try:
        assert await request == 1
    finally:
        await engine.dispose()


def _open_connections(engine: AsyncEngine) -> dict[int, Any]:
    """The DBAPI connections of *engine* that are open now, kept up to date by the pool events."""
    opened: dict[int, Any] = {}
    event.listen(engine.sync_engine, "connect", lambda dbapi, _record: opened.__setitem__(id(dbapi), dbapi))
    event.listen(engine.sync_engine, "close", lambda dbapi, _record: opened.pop(id(dbapi), None))
    event.listen(engine.sync_engine, "close_detached", lambda dbapi: opened.pop(id(dbapi), None))
    return opened


async def test_a_connect_in_flight_across_the_dispose_is_closed_when_returned(tmp_path: Path) -> None:
    """The connection opens in the old pool after the dispose, and goes back there when it is returned."""
    path = tmp_path / "race.db"
    gate = asyncio.Event()
    gate.set()

    async def gated_connect(*_args: Any, **_kwargs: Any) -> Any:
        await gate.wait()
        return await aiosqlite.connect(path)

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", async_creator=gated_connect)
    close_connections_on_return(engine)
    opened = _open_connections(engine)
    holder = await engine.connect()  # the only pooled connection is busy
    await holder.execute(text("SELECT 1"))
    gate.clear()
    request = asyncio.create_task(_select_one(engine))
    await asyncio.sleep(0.05)
    assert not request.done()  # waiting for its connection, in the pool about to be disposed

    await engine.dispose()
    gate.set()
    assert await request == 1
    await holder.close()

    assert opened == {}  # both went back into the disposed pool, and both were closed
    await engine.dispose()


async def test_units_of_work_across_repeated_disposes_leave_no_connection_outside_the_current_pool(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}", pool_size=3, max_overflow=5)
    close_connections_on_return(engine)
    opened = _open_connections(engine)
    stop = asyncio.Event()

    async def worker(seed: int) -> None:
        pause = random.Random(seed)
        while not stop.is_set():
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                await asyncio.sleep(pause.random() * 0.003)
                await conn.execute(text("SELECT 2"))
            await asyncio.sleep(0)

    workers = [asyncio.create_task(worker(seed)) for seed in range(12)]
    for _ in range(5):
        await asyncio.sleep(0.05)
        await engine.dispose()
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.gather(*workers)

    assert len(opened) == engine.sync_engine.pool.checkedin()  # every open connection is an idle one of the pool
    connects: list[object] = []
    event.listen(engine.sync_engine, "connect", lambda *_: connects.append(object()))
    for _ in range(10):
        assert await _select_one(engine) == 1
    assert connects == []  # and the pool keeps pooling
    await engine.dispose()
    assert opened == {}


async def test_the_pool_of_a_connection_record_is_read_from_sqlalchemy(tmp_path: Path) -> None:
    """SQLAlchemy has no public accessor for the pool of a connection record; this pins the attribute the
    hook reads, so an upgrade that renames it fails here instead of silently disabling the hook."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pin.db'}")
    records: list[Any] = []
    event.listen(engine.sync_engine, "checkout", lambda _dbapi, record, _proxy: records.append(record))
    assert await _select_one(engine) == 1
    assert _record_pool(records[0]) is engine.sync_engine.pool
    await engine.dispose()


async def test_without_the_pool_of_a_record_the_hook_falls_back_to_a_generation_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(datasource_registry, "_record_pool", lambda _record: None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fallback.db'}")
    close_connections_on_return(engine)
    closed = _closed_connections(engine)
    async with engine.connect() as conn:
        in_use = await _dbapi_connection(conn)
        await engine.dispose()
        assert in_use not in closed

    assert in_use in closed
    connects: list[object] = []
    event.listen(engine.sync_engine, "connect", lambda *_: connects.append(object()))
    for _ in range(5):
        assert await _select_one(engine) == 1
    assert len(connects) == 1
    await engine.dispose()
