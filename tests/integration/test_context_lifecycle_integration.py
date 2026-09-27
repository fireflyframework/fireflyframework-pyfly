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
  A ``@pre_destroy`` that writes through a ``Provider[AsyncSession]`` or a proxied refresh-scoped
  datasource succeeds too: only singleton creation stops while the singletons are destroyed, and the
  scoped instances are destroyed after them.
- A database that went silent at shutdown (a middlebox black-holes the pooled connections) used to
  keep ``ctx.stop()`` waiting in the driver's close until the kernel gave up on the socket; the close
  is now bounded and the stuck connections are terminated.
- C030/C031/C099: two refresh-scoped datasources of one type, injected into a singleton through a
  scoped proxy, keep their own database across ``POST /actuator/refresh`` cycles, and every refresh
  destroys the evicted datasources, so no pool piles up.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Annotated, Any

import pytest
from sqlalchemy import Integer, String, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container import Provider, Qualifier, bean, configuration, service
from pyfly.container.exceptions import BeanCreationNotAllowedError
from pyfly.container.refresh_scope import REFRESH_SCOPE_NAME, refresh_scope, scoped_proxy
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


def _app_engine(url: str, app_name: str) -> AsyncEngine:
    """An engine of the application's own, visible to :func:`_server_connections` on every lane."""
    if make_url(url).get_backend_name() == "postgresql":
        return create_async_engine(url, connect_args={"server_settings": {"application_name": app_name}})
    return create_async_engine(url)  # MySQL/MariaDB count every connection to the test's database


@service
class _ProviderAuditor:
    def __init__(self, sessions: Provider[AsyncSession]) -> None:
        self._sessions = sessions

    @pre_destroy
    async def flush(self) -> None:
        async with self._sessions.get() as session, session.begin():
            session.add(_ShutdownNote(body="provider-pre_destroy"))
        _OUTCOMES.append("provider-pre_destroy")


_REPORTING: dict[str, str] = {}


@refresh_scope(proxy=True)
class _ReportingSource:
    def __init__(self) -> None:
        self.engine = _app_engine(_REPORTING["url"], _REPORTING["app"])

    async def write(self, body: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO wp07_shutdown_note (body) VALUES (:body)"), {"body": body})

    @pre_destroy
    async def close(self) -> None:
        _OUTCOMES.append("reporting-destroyed")
        await self.engine.dispose()


@service
class _ReportingLedger:
    def __init__(self, reporting: _ReportingSource) -> None:
        self.reporting = reporting

    @pre_destroy
    async def flush(self) -> None:
        await self.reporting.write("proxy-pre_destroy")
        _OUTCOMES.append("proxy-pre_destroy")


async def test_pre_destroy_writes_through_transient_sessions_and_a_proxied_datasource(
    relational_backend: RelationalBackend,
) -> None:
    _OUTCOMES.clear()
    app_name = f"pyfly-predestroy-{uuid.uuid4().hex[:8]}"
    await relational_backend.create_tables(_ShutdownNote)
    _REPORTING.update({"url": relational_backend.url, "app": app_name})
    context = ApplicationContext(relational_backend.config({"pyfly.app.name": app_name}))
    context.register_bean(_ProviderAuditor)
    context.register_bean(_ReportingSource)
    context.register_bean(_ReportingLedger)
    await context.start()
    await context.get_bean(_ReportingLedger).reporting.write("running")

    await context.stop()

    # Both singletons wrote; the scoped datasource was destroyed after them.
    assert sorted(_OUTCOMES[:2]) == ["provider-pre_destroy", "proxy-pre_destroy"]
    assert _OUTCOMES[2:] == ["reporting-destroyed"]
    if not relational_backend.is_embedded:
        assert await _settles_at_zero(relational_backend, app_name) == 0
    assert sorted(await _bodies(relational_backend.url)) == ["provider-pre_destroy", "proxy-pre_destroy", "running"]


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
        relational_backend.config(
            {"pyfly.app.name": app_name, "pyfly.data.relational.url": _proxied(relational_backend, port)}
        )
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
        relational_backend.config(
            {"pyfly.app.name": app_name, "pyfly.data.relational.url": _proxied(relational_backend, port)}
        )
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


# ---------------------------------------------------------------------------
# Refresh: each POST /actuator/refresh destroys the evicted datasource (C099), two refresh-scoped
# datasources of one type keep their own database (C030), and a proxied one follows the refresh (C031).
# ---------------------------------------------------------------------------

_REFRESH_URLS: dict[str, str] = {}


class _ScopedDataSource:
    """A refresh-scoped datasource bean that owns its engine."""

    def __init__(self, url: str, app_name: str) -> None:
        self.engine = create_async_engine(url, connect_args={"server_settings": {"application_name": app_name}})

    async def write(self, body: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("INSERT INTO wp07_shutdown_note (body) VALUES (:body)"), {"body": body})

    @pre_destroy
    async def close(self) -> None:
        await self.engine.dispose()


@configuration
class _RefreshScopedDataSources:
    @scoped_proxy
    @bean(name="orders_source", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def orders_source(self) -> _ScopedDataSource:
        return _ScopedDataSource(_REFRESH_URLS["orders"], _REFRESH_URLS["app"])

    @scoped_proxy
    @bean(name="audit_source", scope=REFRESH_SCOPE_NAME)  # type: ignore[arg-type]
    def audit_source(self) -> _ScopedDataSource:
        return _ScopedDataSource(_REFRESH_URLS["audit"], _REFRESH_URLS["app"])


@service
class _Ledger:
    def __init__(
        self,
        orders: Annotated[_ScopedDataSource, Qualifier("orders_source")],
        audit: Annotated[_ScopedDataSource, Qualifier("audit_source")],
    ) -> None:
        self.orders = orders
        self.audit = audit


@pytest.mark.backends(PG)
async def test_refreshing_scoped_datasources_leaks_no_pool(relational_backend: RelationalBackend) -> None:
    from httpx import ASGITransport, AsyncClient
    from starlette.applications import Starlette

    from pyfly.actuator.adapters.starlette import make_starlette_actuator_routes
    from pyfly.actuator.endpoints.refresh_endpoint import RefreshEndpoint
    from pyfly.actuator.registry import ActuatorRegistry

    app_name = f"pyfly-refresh-{uuid.uuid4().hex[:8]}"
    server = make_url(relational_backend.url)
    audit_name = f"{server.database}_audit"
    audit_url = server.set(database=audit_name).render_as_string(hide_password=False)
    admin = create_async_engine(server.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{audit_name}"'))
    try:
        await relational_backend.create_tables(_ShutdownNote)
        await RelationalBackend(relational_backend.lane, audit_url).create_tables(_ShutdownNote)
        _REFRESH_URLS.update({"orders": relational_backend.url, "audit": audit_url, "app": app_name})

        context = ApplicationContext(relational_backend.config({"pyfly.app.name": f"{app_name}-app"}))
        context.register_bean(_RefreshScopedDataSources)
        context.register_bean(_Ledger)
        await context.start()
        registry = ActuatorRegistry()
        registry.register(RefreshEndpoint(context))
        app = Starlette(routes=make_starlette_actuator_routes(registry))
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://app") as client:
                ledger = context.get_bean(_Ledger)
                for cycle in range(5):
                    await ledger.orders.write(f"order-{cycle}")
                    await ledger.audit.write(f"audit-{cycle}")
                    assert await _server_connections(relational_backend, app_name) == 2
                    response = await client.post("/actuator/refresh")
                    assert response.status_code == 200
                    assert len(response.json()["refreshed"]) == 2
                    # The evicted datasources were destroyed: their pools closed, none piles up.
                    assert await _settles_at_zero(relational_backend, app_name) == 0
        finally:
            await context.stop()

        assert await _server_connections(relational_backend, app_name) == 0
        assert await _bodies(relational_backend.url) == [f"order-{cycle}" for cycle in range(5)]
        assert await _bodies(audit_url) == [f"audit-{cycle}" for cycle in range(5)]
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{audit_name}" WITH (FORCE)'))
        await admin.dispose()


async def _bodies(url: str) -> list[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(select(_ShutdownNote.body).order_by(_ShutdownNote.id))).scalars())
    finally:
        await engine.dispose()
