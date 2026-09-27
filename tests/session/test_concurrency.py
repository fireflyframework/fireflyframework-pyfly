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
"""Session concurrency control (v26.06.55) — max-sessions-per-principal."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from pyfly.session.adapters.memory import InMemorySessionStore
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.concurrency import (
    ConcurrencyControlPolicy,
    InMemorySessionRegistry,
    SessionConcurrencyController,
)
from tests.support.backend_matrix import enable_sqlite_foreign_keys


@pytest.mark.asyncio
async def test_registry_tracks_sessions_oldest_first() -> None:
    reg = InMemorySessionRegistry()
    await reg.register("alice", "s1", 1.0)
    await reg.register("alice", "s2", 2.0)
    await reg.register("bob", "s3", 5.0)

    assert await reg.count("alice") == 2
    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s1", "s2"]  # oldest first

    await reg.deregister("alice", "s1")
    assert await reg.count("alice") == 1
    await reg.deregister("bob", "s3")
    assert await reg.count("bob") == 0  # principal bucket pruned


@pytest.mark.asyncio
async def test_unlimited_always_allows() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=-1))
    for i in range(5):
        assert await ctl.on_login("alice", f"s{i}", float(i)) is True
    assert await reg.count("alice") == 5


@pytest.mark.asyncio
async def test_reject_new_strategy() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=2, strategy="reject-new"))
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is True
    assert await ctl.on_login("alice", "s3", 3.0) is False  # over cap -> rejected
    assert {sid for sid, _ in await reg.list_sessions("alice")} == {"s1", "s2"}  # s3 not registered


@pytest.mark.asyncio
async def test_evict_oldest_strategy_deletes_evicted_session() -> None:
    reg = InMemorySessionRegistry()
    deleted: list[str] = []

    async def _delete(session_id: str) -> None:
        deleted.append(session_id)

    ctl = SessionConcurrencyController(
        reg, ConcurrencyControlPolicy(max_sessions=2, strategy="evict-oldest"), session_deleter=_delete
    )
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is True
    assert await ctl.on_login("alice", "s3", 3.0) is True  # evicts oldest (s1), allows s3

    assert deleted == ["s1"]  # oldest session purged from the store
    assert {sid for sid, _ in await reg.list_sessions("alice")} == {"s2", "s3"}
    assert await reg.count("alice") == 2  # cap held


@pytest.mark.asyncio
async def test_on_logout_deregisters() -> None:
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=5))
    await ctl.on_login("alice", "s1", 1.0)
    await ctl.on_logout("alice", "s1")
    assert await reg.count("alice") == 0


# ---------------------------------------------------------------------------
# Dead sessions and concurrent logins (WP10b: C076, C155)
# ---------------------------------------------------------------------------


class _YieldingRegistry:
    """A :class:`SessionRegistry` with only the four protocol methods, each yielding to the event loop as a
    registry over a network client does."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, float]] = {}

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        await asyncio.sleep(0)
        self.sessions.setdefault(principal, {})[session_id] = created_at

    async def deregister(self, principal: str, session_id: str) -> None:
        await asyncio.sleep(0)
        self.sessions.get(principal, {}).pop(session_id, None)

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        await asyncio.sleep(0)
        return sorted(self.sessions.get(principal, {}).items(), key=lambda kv: kv[1])

    async def count(self, principal: str) -> int:
        await asyncio.sleep(0)
        return len(self.sessions.get(principal, {}))


async def _live(store: InMemorySessionStore, *session_ids: str) -> None:
    for session_id in session_ids:
        await store.save(session_id, {"user": "alice"}, ttl=60)


@pytest.mark.asyncio
async def test_expired_sessions_do_not_count_toward_the_cap() -> None:
    """C076: with reject-new, a user whose max-sessions sessions expired (no logout) was locked out for good."""
    store = InMemorySessionStore()
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(
        reg, ConcurrencyControlPolicy(max_sessions=2, strategy="reject-new"), session_store=store
    )
    await _live(store, "s1", "s2")
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is True
    await store.save("s1", {"user": "alice"}, ttl=-1)  # expired
    await store.delete("s2")  # invalidated by the application: the registry was not told

    assert await ctl.on_login("alice", "s3", 3.0) is True

    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s3"]  # the dead entries were dropped


@pytest.mark.asyncio
async def test_live_sessions_still_count() -> None:
    store = InMemorySessionStore()
    reg = InMemorySessionRegistry()
    ctl = SessionConcurrencyController(
        reg, ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"), session_store=store
    )
    await _live(store, "s1")
    assert await ctl.on_login("alice", "s1", 1.0) is True
    assert await ctl.on_login("alice", "s2", 2.0) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", ["memory", "custom"])
async def test_concurrent_logins_with_eviction_keep_the_cap(registry: str) -> None:
    """C155: evict-oldest evicted from a stale snapshot and registered without re-checking, so concurrent logins
    whose session deletion yields all got in."""
    reg: Any = InMemorySessionRegistry() if registry == "memory" else _YieldingRegistry()
    deleted: list[str] = []

    async def _delete(session_id: str) -> None:
        await asyncio.sleep(0)
        deleted.append(session_id)

    ctl = SessionConcurrencyController(
        reg, ConcurrencyControlPolicy(max_sessions=1, strategy="evict-oldest"), session_deleter=_delete
    )
    assert await ctl.on_login("alice", "s0", 0.0) is True

    results = await asyncio.gather(*(ctl.on_login("alice", f"s{i}", float(i)) for i in range(1, 9)))

    assert all(results)
    assert await reg.count("alice") == 1
    survivors = dict(await reg.list_sessions("alice"))
    assert sorted(deleted) == sorted(f"s{i}" for i in range(9) if f"s{i}" not in survivors)


@pytest.mark.asyncio
async def test_concurrent_logins_with_rejection_admit_one() -> None:
    reg = _YieldingRegistry()
    ctl = SessionConcurrencyController(reg, ConcurrencyControlPolicy(max_sessions=1, strategy="reject-new"))

    results = await asyncio.gather(*(ctl.on_login("alice", f"s{i}", float(i)) for i in range(8)))

    assert results.count(True) == 1
    assert await reg.count("alice") == 1


@pytest.mark.asyncio
async def test_register_limited_is_atomic_in_memory() -> None:
    reg = InMemorySessionRegistry()
    first = await reg.register_limited("alice", "s1", 1.0, max_sessions=1, evict_oldest=False)
    second = await reg.register_limited("alice", "s2", 2.0, max_sessions=1, evict_oldest=False)
    third = await reg.register_limited("alice", "s3", 3.0, max_sessions=1, evict_oldest=True)
    again = await reg.register_limited("alice", "s3", 3.0, max_sessions=1, evict_oldest=False)

    assert (first.accepted, second.accepted, third.accepted, again.accepted) == (True, False, True, True)
    assert third.evicted == ("s1",) and again.evicted == ()
    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s3"]


@pytest.mark.asyncio
async def test_a_cancelled_login_still_deletes_the_session_it_evicted() -> None:
    """The registry commits the eviction before the session is deleted from the store: a login cancelled in
    between (the client went away) left the evicted session usable and no longer counted."""
    store = InMemorySessionStore()
    reg = InMemorySessionRegistry()
    deleting = asyncio.Event()
    release = asyncio.Event()

    async def _delete(session_id: str) -> None:
        deleting.set()
        await release.wait()
        await store.delete(session_id)

    ctl = SessionConcurrencyController(
        reg,
        ConcurrencyControlPolicy(max_sessions=1, strategy="evict-oldest"),
        session_deleter=_delete,
        session_store=store,
    )
    await _live(store, "s1", "s2")
    assert await ctl.on_login("alice", "s1", 1.0) is True
    login = asyncio.create_task(ctl.on_login("alice", "s2", 2.0))
    await deleting.wait()

    login.cancel()
    with pytest.raises(asyncio.CancelledError):
        await login
    release.set()
    await ctl.stop()  # waits for the evictions in flight

    assert await store.get("s1") is None
    assert [sid for sid, _ in await reg.list_sessions("alice")] == ["s2"]


@pytest.mark.asyncio
async def test_a_login_checks_a_bounded_batch_of_registrations(tmp_path: Path) -> None:
    """A login ran a whole purge batch inline (500 liveness checks and a unit per dead session): it now checks
    a small batch, and the next logins take the rest of a backlog."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}")
    enable_sqlite_foreign_keys(engine)
    now = [datetime.now(UTC)]
    registry = PostgresSessionRegistry(engine, ttl=timedelta(seconds=60), clock=lambda: now[0])
    store = InMemorySessionStore()
    ctl = SessionConcurrencyController(
        registry, ConcurrencyControlPolicy(), session_store=store, purge_interval=timedelta(0)
    )
    try:
        for index in range(120):
            await registry.register("alice", f"dead-{index}", float(index))
        now[0] = now[0] + timedelta(seconds=61)

        remaining = []
        for login in range(3):
            await _live(store, f"live-{login}")
            assert await ctl.on_login("alice", f"live-{login}", 1000.0 + login)
            remaining.append(sum(sid.startswith("dead-") for sid, _ in await registry.list_sessions("alice")))

        assert remaining == [70, 20, 0]
    finally:
        await engine.dispose()
