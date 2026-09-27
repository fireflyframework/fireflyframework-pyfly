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
"""``ctx.stop()`` against real servers: the database is released last, completely and on time.

- C033/C101: a ``@pre_destroy`` and a user lifecycle bean write during the stop and succeed, because
  the datasource registry closes after them; afterwards the server holds no connection of the
  application, and nothing (a late ``get_bean(AsyncEngine)``, a bean that kept the engine) reopens one.
- A database that went silent at shutdown (a middlebox black-holes the pooled connections) used to
  keep ``ctx.stop()`` waiting in the driver's close until the kernel gave up on the socket; the close
  is now bounded and the stuck connections are terminated.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import Integer, String, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container import bean, configuration, service
from pyfly.container.exceptions import BeanCreationNotAllowedError
from pyfly.context.application_context import ApplicationContext
from pyfly.context.lifecycle import pre_destroy
from pyfly.data.relational.datasource_registry import DataSourceConfigurationError, DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from tests.support.backend_matrix import PG, RelationalBackend
from tests.support.partition_proxy import PartitionProxy


class _ShutdownNote(Base):
    __tablename__ = "wp07_shutdown_note"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    body: Mapped[str] = mapped_column(String(50))


async def _server_connections(backend: RelationalBackend, app_name: str) -> int:
    """Connections the application holds on the server (``-1`` on SQLite, which has no server)."""
    if backend.is_embedded:
        return -1
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if backend.dialect == "postgresql":
                sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = :app AND pid <> pg_backend_pid()"
                return int((await conn.execute(text(sql), {"app": app_name})).scalar_one())
            database = make_url(backend.url).database
            sql = "SELECT count(*) FROM information_schema.processlist WHERE db = :db AND id <> CONNECTION_ID()"
            return int((await conn.execute(text(sql), {"db": database})).scalar_one())
    finally:
        await engine.dispose()


async def _settles_at_zero(backend: RelationalBackend, app_name: str, *, within: float = 5.0) -> int:
    """The application's connection count once it reaches 0 (a closed backend leaves the view a moment later)."""
    deadline = time.monotonic() + within
    count = await _server_connections(backend, app_name)
    while count > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        count = await _server_connections(backend, app_name)
    return count


async def _insert(factory: async_sessionmaker[AsyncSession], body: str) -> None:
    async with factory() as session, session.begin():
        session.add(_ShutdownNote(body=body))


_OUTCOMES: list[str] = []


@service
class _AuditTrail:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @pre_destroy
    async def flush(self) -> None:
        await _insert(self._factory, "pre_destroy")
        _OUTCOMES.append("pre_destroy")


class _Relay:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def start(self) -> None:
        await _insert(self._factory, "relay-start")

    async def stop(self) -> None:
        await _insert(self._factory, "relay-final")
        _OUTCOMES.append("relay-final")


@configuration
class _RelayConfiguration:
    @bean
    def relay(self, factory: async_sessionmaker[AsyncSession]) -> _Relay:
        return _Relay(factory)


async def test_stop_releases_every_connection_after_the_last_write(relational_backend: RelationalBackend) -> None:
    _OUTCOMES.clear()
    app_name = f"pyfly-stop-{uuid.uuid4().hex[:8]}"
    await relational_backend.create_tables(_ShutdownNote)
    context = ApplicationContext(relational_backend.config({"pyfly.app.name": app_name}))
    context.register_bean(_AuditTrail)
    context.register_bean(_RelayConfiguration)
    await context.start()
    engine = context.get_bean(AsyncEngine)
    factory = context.get_bean(async_sessionmaker)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    if not relational_backend.is_embedded:
        assert await _server_connections(relational_backend, app_name) >= 1

    await context.stop()

    assert _OUTCOMES == ["pre_destroy", "relay-final"]
    if not relational_backend.is_embedded:
        assert await _settles_at_zero(relational_backend, app_name) == 0
    with pytest.raises(BeanCreationNotAllowedError):
        context.get_bean(AsyncEngine)
    with pytest.raises(DataSourceConfigurationError):
        await _insert(factory, "late")
    if not relational_backend.is_embedded:
        assert await _server_connections(relational_backend, app_name) == 0

    check = create_async_engine(relational_backend.url, poolclass=NullPool)
    try:
        async with check.connect() as conn:
            bodies = list((await conn.execute(select(_ShutdownNote.body).order_by(_ShutdownNote.id))).scalars())
    finally:
        await check.dispose()
    assert bodies == ["relay-start", "pre_destroy", "relay-final"]


def _proxied(backend: RelationalBackend, port: int) -> str:
    return make_url(backend.url).set(host="127.0.0.1", port=port).render_as_string(hide_password=False)


async def _proxy(backend: RelationalBackend) -> tuple[PartitionProxy, int]:
    upstream = make_url(backend.url)
    proxy = PartitionProxy(upstream.host or "127.0.0.1", int(upstream.port or 5432))
    return proxy, await proxy.start()


async def _touch(engine: AsyncEngine, connections: int) -> None:
    held: list[Any] = []
    try:
        for _ in range(connections):
            conn = await engine.connect()
            held.append(conn)
            await conn.execute(text("SELECT 1"))
    finally:
        for conn in held:
            await conn.close()


@pytest.mark.backends(PG)
async def test_close_is_bounded_when_the_database_black_holes_the_pool(relational_backend: RelationalBackend) -> None:
    app_name = f"pyfly-blackhole-{uuid.uuid4().hex[:8]}"
    proxy, port = await _proxy(relational_backend)
    registry = DataSourceRegistry(
        relational_backend.config({"pyfly.app.name": app_name, "pyfly.data.relational.url": _proxied(relational_backend, port)})
    )
    engine = registry.primary.engine
    try:
        await _touch(engine, 2)
        assert engine.pool.checkedin() == 2
        assert await _server_connections(relational_backend, app_name) == 2

        # A middlebox forgets both idle flows: the close of each pooled connection gets no answer.
        proxy.black_hole_established()
        started = time.monotonic()
        await asyncio.wait_for(registry.close(timeout=1.0), 10)
        assert time.monotonic() - started < 5

        # The stuck connections were terminated, so the server sees them go.
        assert await _settles_at_zero(relational_backend, app_name) == 0
        with pytest.raises(DataSourceConfigurationError):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    finally:
        await registry.close()
        await proxy.close()


@pytest.mark.backends(PG)
async def test_ctx_stop_ends_on_time_when_the_database_black_holes_the_pool(
    relational_backend: RelationalBackend,
) -> None:
    app_name = f"pyfly-ctxstop-{uuid.uuid4().hex[:8]}"
    proxy, port = await _proxy(relational_backend)
    context = ApplicationContext(
        relational_backend.config({"pyfly.app.name": app_name, "pyfly.data.relational.url": _proxied(relational_backend, port)})
    )
    await context.start()
    try:
        await _touch(context.get_bean(AsyncEngine), 1)
        assert await _server_connections(relational_backend, app_name) == 1

        proxy.black_hole_established()
        started = time.monotonic()
        await asyncio.wait_for(context.stop(), 15)
        assert time.monotonic() - started < 10

        assert await _settles_at_zero(relational_backend, app_name) == 0
    finally:
        await proxy.close()
