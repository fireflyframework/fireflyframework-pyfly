from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_two_sql_stores_share_binding_capacity_delete_and_expiry(tmp_path):
    from pyfly.mcp import McpSessionPrincipal, SqlMcpSessionStore

    now = [datetime(2026, 10, 31, tzinfo=UTC)]
    url = f"sqlite+aiosqlite:///{tmp_path / 'metadata.db'}"
    engines = [create_async_engine(url) for _ in range(2)]
    stores = [SqlMcpSessionStore(engine, clock=lambda: now[0]) for engine in engines]
    alice = McpSessionPrincipal("credential-a", "https://service.test/mcp", now[0] + timedelta(hours=1))
    bob = McpSessionPrincipal("credential-b", alice.resource, alice.expires_at)
    params = {
        "protocolVersion": "2025-11-25",
        "clientInfo": {"name": "test", "version": "1"},
        "capabilities": {"extensions": {"ui": {}}},
    }
    try:
        await stores[0].start()
        await stores[1].start()
        opened = await stores[0].create(alice, "2025-11-25", params, ttl=30, max_sessions=1)
        assert opened
        assert await stores[1].get(opened, bob, ttl=30) is None
        assert not await stores[1].delete(opened, bob)
        assert await stores[1].create(bob, "2025-11-25", params, ttl=30, max_sessions=1) is None
        assert await stores[1].initialize(opened, alice)
        row = await stores[0].get(opened, alice, ttl=30)
        assert row.initialized and row.client_params == params
        assert await stores[1].delete(opened, alice)
        assert await stores[0].get(opened, alice, ttl=30) is None
        assert not await stores[0].initialize(opened, alice)
        second = await stores[1].create(alice, "2025-11-25", params, ttl=30, max_sessions=1)
        now[0] += timedelta(seconds=31)
        assert await stores[0].get(second, alice, ttl=30) is None
        assert not await stores[0].initialize(second, alice)
        assert await stores[0].create(alice, "2025-11-25", params, ttl=30, max_sessions=1)
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_atomic_admission_is_global_across_sql_store_instances(tmp_path):
    from pyfly.mcp import McpSessionPrincipal, SqlMcpSessionStore

    url = f"sqlite+aiosqlite:///{tmp_path / 'admission.db'}"
    engines = [create_async_engine(url) for _ in range(2)]
    stores = [SqlMcpSessionStore(engine) for engine in engines]
    principal = McpSessionPrincipal("credential-a", "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1))
    try:
        for store in stores:
            await store.start()
        admitted = await asyncio.gather(
            *(store.create(principal, "2025-11-25", {}, ttl=30, max_sessions=1) for store in stores)
        )
        assert sum(value is not None for value in admitted) == 1
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_metadata_uses_registered_primary_without_opening_replica_or_provider(tmp_path):
    from pyfly.core.config import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.mcp import McpSessionPrincipal, SqlMcpSessionStore

    paths = {name: tmp_path / f"{name}.db" for name in ("primary", "replica", "provider")}
    registry = DataSourceRegistry.for_config(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "enabled": True,
                            "url": f"sqlite+aiosqlite:///{paths['primary']}",
                            "read-replica": {"url": f"sqlite+aiosqlite:///{paths['replica']}"},
                            "datasources": {"provider": {"url": f"sqlite+aiosqlite:///{paths['provider']}"}},
                        }
                    }
                }
            }
        )
    )
    try:
        store = SqlMcpSessionStore(registry.primary)
        principal = McpSessionPrincipal("grant", "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1))
        session = await store.create(principal, "2025-11-25", {}, ttl=60, max_sessions=2)
        assert await store.initialize(session, principal)
        assert (await store.get(session, principal, ttl=60)).initialized
        assert await store.delete(session, principal)
        assert paths["primary"].exists()
        assert not paths["replica"].exists() and not paths["provider"].exists()
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_delete_cannot_be_undone_by_concurrent_touches_and_future_schema_is_rejected(tmp_path):
    from sqlalchemy import update

    from pyfly.data.relational.framework_schema import mcp_sessions
    from pyfly.data.transaction import TransactionTemplate
    from pyfly.mcp import McpSessionPrincipal, SqlMcpSessionStore

    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engines = [create_async_engine(url) for _ in range(2)]
    stores = [SqlMcpSessionStore(engine) for engine in engines]
    principal = McpSessionPrincipal("grant", "https://service.test/mcp", datetime.now(UTC) + timedelta(hours=1))
    try:
        for store in stores:
            await store.start()
        identifier = await stores[0].create(principal, "2025-11-25", {}, ttl=60, max_sessions=2)
        await asyncio.gather(stores[0].get(identifier, principal, ttl=60), stores[1].delete(identifier, principal))
        assert await stores[0].get(identifier, principal, ttl=60) is None
        identifier = await stores[0].create(principal, "2025-11-25", {}, ttl=60, max_sessions=2)
        async with TransactionTemplate(engines[0]).transaction() as unit:
            await unit.resource.execute(
                update(mcp_sessions).where(mcp_sessions.c.session_id == identifier).values(schema_version=999)
            )
        assert await stores[1].get(identifier, principal, ttl=60) is None
        assert not await stores[1].initialize(identifier, principal)
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.mark.asyncio
async def test_expiry_between_lookup_and_touch_cannot_release_or_revive_metadata(tmp_path):
    from sqlalchemy import event

    from pyfly.mcp import McpSessionPrincipal, SqlMcpSessionStore

    now = [datetime(2026, 10, 31, tzinfo=UTC)]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'expiry-cut.db'}")
    store = SqlMcpSessionStore(engine, clock=lambda: now[0])
    principal = McpSessionPrincipal("grant", "https://service.test/mcp", now[0] + timedelta(hours=1))
    try:
        identifier = await store.create(principal, "2025-11-25", {}, ttl=1, max_sessions=1)

        def expires_after_read(connection, cursor, statement, parameters, context, many):
            if statement.startswith("SELECT") and "pyfly_mcp_sessions" in statement:
                now[0] += timedelta(seconds=2)

        event.listen(engine.sync_engine, "after_cursor_execute", expires_after_read)
        assert await store.get(identifier, principal, ttl=30) is None
        assert await store.get(identifier, principal, ttl=30) is None
    finally:
        await engine.dispose()
