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
"""The datasource registry on every backend of the matrix (SQLite file, PostgreSQL, MySQL, MariaDB).

- Proof p9 (F9): an application with a replica, a named datasource, the SQL event store, snapshots, saga
  persistence and (on PostgreSQL) the SQL cache, all against one database, opens three pools, not
  seven; every pool gets the configured settings; after ``ctx.stop()`` the server holds no connection
  of the application.
- F13: connect arguments reach the driver, and PostgreSQL connections carry the application name.
- C045: a rotated password reaches new connections without a restart, and a configuration refresh
  evicts the pools opened with the old one.
- The after-begin customizer SPI sets a transaction-local GUC inside the unit (the dworkers tenant
  GUC, the reference customer).
- Capabilities are final once the dialect has met the server (MariaDB 11 has ``INSERT ... RETURNING``,
  MySQL 8 has none).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.context.application_context import ApplicationContext
from pyfly.context.refresh import ContextRefresher
from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry
from tests.support.backend_matrix import MARIADB, MYSQL, PG, SQLITE_FILE, RelationalBackend


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


async def _touch(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def test_one_pool_per_database_every_pool_configured_every_pool_disposed(
    relational_backend: RelationalBackend,
) -> None:
    app_name = f"pyfly-p9-{uuid.uuid4().hex[:8]}"
    url = relational_backend.url
    overrides: dict[str, Any] = {
        "pyfly.app.name": app_name,
        "pyfly.data.relational.pool.size": "3",
        "pyfly.data.relational.pool.max-overflow": "2",
        "pyfly.data.relational.read-replica.url": url,
        "pyfly.data.relational.datasources.reporting.url": url,
        "pyfly.eventsourcing.enabled": "true",
        "pyfly.eventsourcing.store.provider": "sqlalchemy",
        "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
        "pyfly.eventsourcing.snapshot.url": url,
        "pyfly.transactional.enabled": "true",
        "pyfly.transactional.persistence.provider": "sqlalchemy",
    }
    if relational_backend.dialect == "postgresql":
        overrides.update(
            {"pyfly.cache.enabled": "true", "pyfly.cache.provider": "postgres", "pyfly.cache.postgres.url": url}
        )
    context = ApplicationContext(relational_backend.config(overrides))
    await context.start()
    registry = context.get_bean(DataSourceRegistry)
    engines = [ds.engine for ds in registry.all_datasources()]
    disposed: list[int] = []
    for engine in engines:
        event.listen(engine.sync_engine, "engine_disposed", lambda e: disposed.append(id(e)))
    try:
        assert [ds.qualified_name for ds in registry.all_datasources()] == ["primary", "primary.replica", "reporting"]
        primary = context.get_bean(AsyncEngine)
        from pyfly.eventsourcing.snapshot import SnapshotStore
        from pyfly.eventsourcing.store import EventStore
        from pyfly.transactional.core.persistence import ExecutionPersistenceProvider

        stores = [context.get_bean(EventStore), context.get_bean(SnapshotStore)]
        stores.append(context.get_bean(ExecutionPersistenceProvider))
        if relational_backend.dialect == "postgresql":
            from pyfly.cache.ports.outbound import CacheAdapter

            stores.append(context.get_bean(CacheAdapter))
        assert all(store._engine is primary for store in stores)  # type: ignore[attr-defined]

        for engine in engines:
            assert engine.pool.size() == 3
            assert engine.pool._max_overflow == 2
            assert engine.pool._recycle == 1800
            assert engine.pool._pre_ping is relational_backend.pre_ping
            await _touch(engine)
        if not relational_backend.is_embedded:
            assert await _server_connections(relational_backend, app_name) >= 3
    finally:
        await context.stop()

    assert sorted(disposed) == sorted(id(engine.sync_engine) for engine in engines)
    if not relational_backend.is_embedded:
        assert await _server_connections(relational_backend, app_name) == 0


@pytest.mark.backends(PG)
async def test_connect_args_and_application_name_reach_postgresql(relational_backend: RelationalBackend) -> None:
    registry = DataSourceRegistry(
        relational_backend.config(
            {
                "pyfly.app.name": "orders-service",
                "pyfly.data.relational.connect-args.statement_cache_size": 0,
                "pyfly.data.relational.connect-args.server_settings.search_path": "pg_catalog",
            }
        )
    )
    try:
        async with registry.primary.engine.connect() as conn:
            assert (await conn.execute(text("SHOW search_path"))).scalar_one() == "pg_catalog"
            assert (await conn.execute(text("SHOW application_name"))).scalar_one() == "orders-service"
    finally:
        await registry.close()


@pytest.mark.backends(SQLITE_FILE, PG, MYSQL, MARIADB)
async def test_capabilities_are_final_after_the_first_connection(relational_backend: RelationalBackend) -> None:
    registry = DataSourceRegistry(relational_backend.config())
    try:
        datasource = registry.primary
        await _touch(datasource.engine)
        caps = datasource.capabilities
        assert caps is datasource.capabilities  # cached once the dialect met the server
        assert caps.supports_savepoints is True
        assert caps.fast_autocommit_reads is (relational_backend.lane == PG)
        if relational_backend.lane == PG:
            assert caps.dialect == "postgresql" and caps.supports_returning
            assert "READ UNCOMMITTED" not in caps.isolation_levels  # asyncpg has none
        elif relational_backend.lane == MYSQL:
            assert caps.dialect == "mysql" and not caps.insert_returning
        elif relational_backend.lane == MARIADB:
            assert caps.dialect == "mariadb" and caps.insert_returning and caps.delete_returning
        else:
            assert caps.dialect == "sqlite" and caps.insert_returning
    finally:
        await registry.close()


# ---------------------------------------------------------------------------
# PostgreSQL: credential rotation (C045) and the after-begin SPI
# ---------------------------------------------------------------------------


async def _admin(url: str, *statements: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _count_backends(admin_url: str, role: str) -> int:
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            sql = "SELECT count(*) FROM pg_stat_activity WHERE usename = :role"
            return int((await conn.execute(text(sql), {"role": role})).scalar_one())
    finally:
        await engine.dispose()


async def _current_user(factory: Any) -> str:
    async with factory() as session:
        return str((await session.execute(text("SELECT current_user"))).scalar_one())


@pytest.mark.backends(PG)
async def test_rotated_password_reaches_new_connections_and_refresh_evicts_the_old_ones(
    relational_backend: RelationalBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_url = relational_backend.url
    role = f"rot_{uuid.uuid4().hex[:10]}"
    base = make_url(admin_url)
    templated = base.set(username=role, password="PLACEHOLDER").render_as_string(hide_password=False)
    templated = templated.replace("PLACEHOLDER", "${ROTATION_TEST_PASSWORD}")
    await _admin(admin_url, f"CREATE ROLE {role} LOGIN PASSWORD 'old-secret'")
    monkeypatch.setenv("ROTATION_TEST_PASSWORD", "old-secret")
    context = ApplicationContext(
        relational_backend.config(
            {
                "pyfly.data.relational.url": templated,
                "pyfly.data.relational.datasources.reporting.url": templated,
            }
        )
    )
    await context.start()
    try:
        registry = context.get_bean(DataSourceRegistry)
        reporting = registry.get("reporting")
        assert await _current_user(registry.primary.sessionmaker) == role
        assert await _current_user(reporting.sessionmaker) == role  # the named URL's placeholder resolved

        # The operator rotates the password (Vault, a secrets manager) and the process sees the new one.
        await _admin(admin_url, f"ALTER ROLE {role} PASSWORD 'new-secret'")
        monkeypatch.setenv("ROTATION_TEST_PASSWORD", "new-secret")

        # A refresh evicts the pools opened with the old password...
        evicted = await context.get_bean(ContextRefresher).refresh()
        assert evicted == []  # no refresh-scoped bean; the pools are the registry's
        assert await _count_backends(admin_url, role) == 0

        # ...and every new connection authenticates with the new one, on every datasource.
        assert await _current_user(registry.primary.sessionmaker) == role
        assert await _current_user(reporting.sessionmaker) == role

        # A server-side drop (lease revoked, failover): the pool reconnects with the live password.
        await _admin(admin_url, f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'")
        outcomes: list[str] = []
        for _ in range(2):
            try:
                outcomes.append(await _current_user(registry.primary.sessionmaker))
                break
            except DBAPIError as exc:  # the dead pooled connection is invalidated on first use
                outcomes.append(type(exc.orig).__name__)
        assert outcomes[-1] == role
    finally:
        await context.stop()
        await _admin(
            admin_url,
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{role}'",
            f"DROP ROLE IF EXISTS {role}",
        )


class RoleProvider:
    """A ``DataSourceCredentialsProvider`` that answers for some datasources and records who asked."""

    def __init__(self, answers: dict[str, tuple[str, str]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def datasource_credentials(self, datasource: str) -> tuple[str | None, str | None] | None:
        self.asked.append(datasource)
        return self.answers.get(datasource)


@pytest.mark.backends(PG)
async def test_credentials_provider_tells_a_replica_from_its_primary(relational_backend: RelationalBackend) -> None:
    # IAM tokens are scoped to a host, and a replica often logs in as a read-only role: a provider must be
    # able to answer for the replica apart from its primary.
    admin_url = relational_backend.url
    suffix = uuid.uuid4().hex[:8]
    writer, reader, token = f"rw_{suffix}", f"ro_{suffix}", f"tk_{suffix}"
    await _admin(
        admin_url,
        f"CREATE ROLE {writer} LOGIN PASSWORD 'rw-secret'",
        f"CREATE ROLE {reader} LOGIN PASSWORD 'ro-secret'",
        f"CREATE ROLE {token} LOGIN PASSWORD 'token-secret'",
    )
    base = make_url(admin_url)
    config = {
        "pyfly.data.relational.url": base.set(username=writer, password="rw-secret").render_as_string(
            hide_password=False
        ),
        "pyfly.data.relational.read-replica.url": base.set(username=reader, password="ro-secret").render_as_string(
            hide_password=False
        ),
    }
    try:
        # A provider that answers for the primary only leaves the replica on its own credentials.
        registry = DataSourceRegistry(relational_backend.config(config))
        primary_only = RoleProvider({"primary": (writer, "rw-secret")})
        registry.add_credentials_provider(primary_only)
        try:
            replica = registry.replica()
            assert replica is not None
            assert await _current_user(registry.primary.sessionmaker) == writer
            assert await _current_user(replica.sessionmaker) == reader
            assert primary_only.asked == ["primary", "primary.replica"]
        finally:
            await registry.close()

        # A provider that answers for the replica reaches the replica, and only the replica.
        registry = DataSourceRegistry(relational_backend.config(config))
        registry.add_credentials_provider(RoleProvider({"primary.replica": (token, "token-secret")}))
        try:
            replica = registry.replica()
            assert replica is not None
            assert await _current_user(replica.sessionmaker) == token
            assert await _current_user(registry.primary.sessionmaker) == writer
        finally:
            await registry.close()
    finally:
        await _admin(
            admin_url,
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename IN ('{writer}', '{reader}', '{token}')",
            f"DROP ROLE IF EXISTS {writer}",
            f"DROP ROLE IF EXISTS {reader}",
            f"DROP ROLE IF EXISTS {token}",
        )


_tenant: ContextVar[str | None] = ContextVar("test_tenant", default=None)


class TenantGuc:
    """The reference customizer: a transaction-local tenant GUC, skipped when no tenant is set."""

    async def after_begin(self, connection: AsyncSession | AsyncConnection, datasource: DataSource) -> None:
        tenant = _tenant.get()
        if tenant is not None:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant})


@pytest.mark.backends(PG)
async def test_after_begin_customizer_sets_a_transaction_local_guc(relational_backend: RelationalBackend) -> None:
    registry = DataSourceRegistry(relational_backend.config({"pyfly.data.relational.pool.size": "1"}))
    registry.add_customizer(TenantGuc())
    datasource = registry.primary

    async def tenant_in_unit() -> str | None:
        async with datasource.sessionmaker() as session, session.begin():
            await session.connection(execution_options=datasource.begin_options(read_only=False))
            await datasource.after_begin(session)
            value = (await session.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar()
            return value or None

    try:
        token = _tenant.set("acme")
        try:
            assert await tenant_in_unit() == "acme"
        finally:
            _tenant.reset(token)
        # The same pooled connection (pool size 1), a new transaction: the GUC did not leak.
        assert await tenant_in_unit() is None
        async with datasource.engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar() in (None, "")
    finally:
        await registry.close()
