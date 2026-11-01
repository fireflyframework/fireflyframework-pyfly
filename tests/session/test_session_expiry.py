# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Expiry observations do not renew sessions or borrow stale replica state."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.adapters.redis import RedisSessionStore
from pyfly.session.adapters.sql_session_store import SqlSessionStore


async def test_memory_expiry_observes_remaining_ttl_without_renewing(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.session import SessionExpiryStore

    now = [datetime(2026, 11, 1, tzinfo=UTC)]
    ticks = [1000.0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr("pyfly.session.adapters.memory.datetime", Clock)
    monkeypatch.setattr("pyfly.session.adapters.memory.time.monotonic", lambda: ticks[0])
    store = InMemorySessionStore()
    assert isinstance(store, SessionExpiryStore)
    assert await store.expires_at("missing") is None
    await store.save("session", {"private": "unchanged"}, 60)
    deadline = now[0] + timedelta(seconds=60)
    assert await store.expires_at("session") == deadline
    ticks[0] += 30
    now[0] += timedelta(seconds=30)
    assert await store.expires_at("session") == deadline
    assert await store.get("session") == {"private": "unchanged"}
    ticks[0] += 31
    now[0] += timedelta(seconds=31)
    assert await store.expires_at("session") is None
    assert not await store.exists("session")


async def test_sql_two_instances_observe_renewal_rotation_and_revocation(tmp_path: Path) -> None:
    from pyfly.session import SessionExpiryStore

    now = [datetime(2026, 11, 1, tzinfo=UTC)]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    first = SqlSessionStore(engine, clock=lambda: now[0], purge_interval=None)
    second = SqlSessionStore(engine, clock=lambda: now[0], purge_interval=None)
    try:
        assert isinstance(second, SessionExpiryStore)
        assert await second.expires_at("missing") is None
        await first.save("original", {"private": "unchanged"}, 60)
        deadline = now[0] + timedelta(seconds=60)
        assert await second.expires_at("original") == deadline
        now[0] += timedelta(seconds=30)
        assert await second.expires_at("original") == deadline
        assert await first.get("original") == {"private": "unchanged"}
        assert await first.rename("original", "rotated", {"private": "unchanged"}, 120)
        assert await second.expires_at("original") is None
        assert await second.expires_at("rotated") == now[0] + timedelta(seconds=120)
        await first.delete("rotated")
        assert await second.expires_at("rotated") is None
        assert not await first.replace("rotated", {}, 120)
        await first.save("expired", {}, 5)
        now[0] += timedelta(seconds=6)
        assert await second.expires_at("expired") is None
    finally:
        await engine.dispose()


async def test_sql_expiry_uses_primary_inside_replica_unit(tmp_path: Path) -> None:
    from pyfly.core import Config
    from pyfly.data.relational.datasource_registry import DataSourceRegistry
    from pyfly.data.transaction import TransactionTemplate

    now = datetime(2026, 11, 1, tzinfo=UTC)
    registry = DataSourceRegistry(
        Config(
            {
                "pyfly": {
                    "data": {
                        "relational": {
                            "url": f"sqlite+aiosqlite:///{tmp_path / 'primary.db'}",
                            "read-replica": {"url": f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}"},
                        }
                    }
                }
            }
        )
    )
    try:
        primary = SqlSessionStore(registry.primary, clock=lambda: now, purge_interval=None)
        replica = registry.replica()
        assert replica is not None
        stale_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replica.db'}")
        stale = SqlSessionStore(stale_engine, clock=lambda: now, purge_interval=None)
        await primary.save("revoked", {}, 60)
        try:
            await stale.save("revoked", {}, 600)
        finally:
            await stale_engine.dispose()
        await primary.delete("revoked")
        await primary.save("live", {}, 60)
        async with TransactionTemplate(registry.primary, read_only=True).transaction():
            assert await primary.expires_at("revoked") is None
            assert await primary.expires_at("live") == now + timedelta(seconds=60)
    finally:
        await registry.close()


@pytest.mark.parametrize("raw", [None, [], [-1], [-2]])
async def test_redis_missing_or_unbounded_expiry_stays_unknown(raw) -> None:
    client = AsyncMock()
    client.register_script = lambda script: AsyncMock(return_value=raw)
    assert await RedisSessionStore(client).expires_at("session") is None


async def test_redis_expiry_uses_server_time_without_returning_session_data() -> None:
    client = AsyncMock()
    query = AsyncMock(return_value=[1793491200, 123000, 59500])
    client.register_script = lambda script: query
    result = await RedisSessionStore(client).expires_at("private-session-id")
    assert result == datetime.fromtimestamp(1793491200.123, UTC) + timedelta(milliseconds=59500)
    query.assert_awaited_once_with(keys=["pyfly:session:private-session-id"])
    client.set.assert_not_awaited()
    client.get.assert_not_awaited()


def test_expiry_is_optional_for_existing_custom_stores() -> None:
    from pyfly.session import SessionExpiryStore, SessionStore

    class CustomStore:
        get = AsyncMock()
        save = AsyncMock()
        delete = AsyncMock()
        exists = AsyncMock()

    assert isinstance(CustomStore(), SessionStore)
    assert not isinstance(CustomStore(), SessionExpiryStore)


def test_authentication_observation_changes_only_on_fresh_login_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    from pyfly.session import HttpSession

    session = HttpSession("original", {"private": "unchanged"})
    assert session.authenticated_at is None
    monkeypatch.setattr("pyfly.session.session.time.time", lambda: 1793491200.25)
    session.set_attribute("oauth2_state", "synthetic-state")
    session.rotate_id()
    assert session.authenticated_at is None
    session.rotate_id(on_login=True)
    assert session.authenticated_at == 1793491200.25
    session.mark_persisted()
    reloaded = HttpSession(session.id, session.get_data())
    assert reloaded.authenticated_at == session.authenticated_at
    assert "_authenticated_at" not in reloaded.get_attribute_names()
    monkeypatch.setattr("pyfly.session.session.time.time", lambda: 1793491800.5)
    reloaded.rotate_id()
    assert reloaded.authenticated_at == 1793491200.25
    reloaded.rotate_id(on_login=True)
    assert reloaded.authenticated_at == 1793491800.5
