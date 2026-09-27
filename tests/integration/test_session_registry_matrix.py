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
"""The SQL session registry and session store on every relational lane (WP10b: C076, C154, C155).

- C155: the cap was a list in one transaction and a register in another, so max-sessions=1 let N concurrent
  logins in, within a process and across replicas. The check and the registration are now one unit that
  holds the principal's row.
- C076: nothing removed the registration of a session that expired: reject-new locked the user out for good,
  and the table grew forever. Dead sessions are dropped at the next login and by a purge.
- C154: the "relational, cross-process" registry had no relational session store behind it, so evicting a
  session held by another replica did nothing. ``SqlSessionStore`` is that store.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pyfly.session.adapters.sql_session_store import SqlSessionStore
from sqlalchemy import func, select

from pyfly.data.relational.framework_schema import FrameworkSchemaError, session_registrations, sessions
from pyfly.security.context import SecurityContext
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.concurrency import ConcurrencyControlPolicy, SessionConcurrencyController
from tests.integration._repository_harness import repository_datasources
from tests.support.backend_matrix import RelationalBackend

CONCURRENCY = 20


@contextlib.asynccontextmanager
async def _replicas(backend: RelationalBackend, count: int = 2, **options: Any) -> AsyncIterator[list[Any]]:
    """*count* replicas of one application on one database: each with its own engine, SQL session store,
    SQL registry and controller."""
    async with repository_datasources(backend) as datasources:
        replicas = [datasources.registry.primary] + [backend.create_engine() for _ in range(count - 1)]
        built = []
        for target in replicas:
            store = SqlSessionStore(target)
            registry = PostgresSessionRegistry(target, **options)
            await store.start()
            await registry.start()
            built.append((store, registry, datasources.engine))
        yield built


def _controller(
    store: SqlSessionStore, registry: PostgresSessionRegistry, **policy: Any
) -> SessionConcurrencyController:
    return SessionConcurrencyController(
        registry, ConcurrencyControlPolicy(**policy), session_deleter=store.delete, session_store=store
    )


async def _login(controller: SessionConcurrencyController, store: SqlSessionStore, principal: str, sid: str) -> bool:
    await store.save(sid, {"user": principal}, ttl=600)
    return await controller.on_login(principal, sid, time.time())


# ---------------------------------------------------------------------------------------------------------
# The cap under concurrency (C155)
# ---------------------------------------------------------------------------------------------------------


async def test_reject_new_admits_one_of_n_concurrent_logins_across_replicas(
    relational_backend: RelationalBackend,
) -> None:
    async with _replicas(relational_backend) as replicas:
        controllers = [
            (_controller(store, registry, max_sessions=1, strategy="reject-new"), store)
            for store, registry, _ in replicas
        ]

        results = await asyncio.gather(*(_login(*controllers[i % 2], "alice", f"s{i}") for i in range(CONCURRENCY)))

        assert results.count(True) == 1
        assert await replicas[0][1].count("alice") == 1


async def test_evict_oldest_keeps_the_cap_under_concurrent_logins(relational_backend: RelationalBackend) -> None:
    async with _replicas(relational_backend) as replicas:
        controllers = [(_controller(store, registry, max_sessions=1), store) for store, registry, _ in replicas]

        results = await asyncio.gather(*(_login(*controllers[i % 2], "alice", f"s{i}") for i in range(CONCURRENCY)))

        assert all(results)
        registry, store = replicas[0][1], replicas[0][0]
        survivors = [sid for sid, _ in await registry.list_sessions("alice")]
        assert len(survivors) == 1
        # Every other session was evicted from the shared store too: only the survivor still resolves.
        resolving = [f"s{i}" for i in range(CONCURRENCY) if await store.get(f"s{i}") is not None]
        assert resolving == survivors


async def test_eviction_reaches_a_session_held_by_another_replica(relational_backend: RelationalBackend) -> None:
    """C154: with a process-local store, a login on replica B "evicted" A's session by its registry row only,
    and the session stayed usable on A."""
    async with _replicas(relational_backend) as replicas:
        (store_a, registry_a, _), (store_b, registry_b, _) = replicas
        on_a = _controller(store_a, registry_a, max_sessions=1)
        on_b = _controller(store_b, registry_b, max_sessions=1)

        assert await _login(on_a, store_a, "bob", "session-a")
        assert await _login(on_b, store_b, "bob", "session-b")

        assert await store_a.get("session-a") is None
        assert await store_a.get("session-b") is not None


async def test_principals_and_session_ids_compare_exactly(relational_backend: RelationalBackend) -> None:
    """On MySQL and MariaDB ``bob`` and ``Bob`` were one principal: one's logins evicted the other's."""
    async with _replicas(relational_backend, count=1) as [(store, registry, _engine)]:
        controller = _controller(store, registry, max_sessions=1, strategy="reject-new")

        assert await _login(controller, store, "bob", "s-lower")
        assert await _login(controller, store, "Bob", "s-upper")
        assert await _login(controller, store, "BOB", "S-LOWER")

        assert [sid for sid, _ in await registry.list_sessions("Bob")] == ["s-upper"]
        assert await store.get("S-LOWER") is not None and await store.get("s-lower") is not None


# ---------------------------------------------------------------------------------------------------------
# Dead sessions (C076)
# ---------------------------------------------------------------------------------------------------------


async def test_expired_sessions_no_longer_lock_the_user_out(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with repository_datasources(relational_backend) as datasources:
        store = SqlSessionStore(datasources.registry.primary, clock=lambda: now[0])
        registry = PostgresSessionRegistry(datasources.registry.primary)
        controller = _controller(store, registry, max_sessions=2, strategy="reject-new")
        assert await _login(controller, store, "carol", "s1")
        assert await _login(controller, store, "carol", "s2")
        assert not await _login(controller, store, "carol", "s3")

        now[0] = now[0] + timedelta(seconds=601)  # both sessions expired, nobody logged out

        assert await _login(controller, store, "carol", "s4")
        assert [sid for sid, _ in await registry.list_sessions("carol")] == ["s4"]


async def test_a_purge_drops_dead_registrations_and_keeps_live_ones(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with repository_datasources(relational_backend) as datasources:
        target = datasources.registry.primary
        store = SqlSessionStore(target)
        registry = PostgresSessionRegistry(target, ttl=timedelta(seconds=60), clock=lambda: now[0])
        controller = _controller(store, registry, max_sessions=-1)
        for sid in ("live", "gone-1", "gone-2"):
            assert await _login(controller, store, "dave", sid)
        await store.delete("gone-1")
        await store.delete("gone-2")

        assert await controller.purge_expired() == 0  # no registration has reached its expiry yet

        now[0] = now[0] + timedelta(seconds=61)
        assert await controller.purge_expired() == 2

        assert [sid for sid, _ in await registry.list_sessions("dave")] == ["live"]
        async with datasources.engine.connect() as connection:
            expires_at = (
                await connection.execute(
                    select(session_registrations.c.expires_at).where(session_registrations.c.session_id == "live")
                )
            ).scalar_one()
        assert expires_at > now[0]  # the live session's registration was renewed, not dropped


async def test_the_registry_round_trip(relational_backend: RelationalBackend) -> None:
    async with _replicas(relational_backend, count=1) as [(_store, registry, _engine)]:
        await registry.register("alice", "s2", 2.0)
        await registry.register("alice", "s1", 1.0)  # older score, inserted second
        assert await registry.count("alice") == 2
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["s1", "s2"]  # oldest first
        assert [created for _, created in await registry.list_sessions("alice")] == [1.0, 2.0]

        await registry.register("alice", "s2", 9.0)  # re-registering one session id does not duplicate it
        assert await registry.count("alice") == 2

        await registry.deregister("alice", "s1")
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["s2"]


# ---------------------------------------------------------------------------------------------------------
# The SQL session store
# ---------------------------------------------------------------------------------------------------------


async def test_the_session_store_round_trip(relational_backend: RelationalBackend) -> None:
    async with _replicas(relational_backend, count=1) as [(store, _registry, _engine)]:
        context = SecurityContext(user_id="erin", roles=["ADMIN"], attributes={"email": "e@example.com"})
        await store.save("sid", {"SECURITY_CONTEXT": context, "n": 1, "_created_at": 1.5}, ttl=60)

        loaded = await store.get("sid")
        assert loaded is not None
        assert loaded["SECURITY_CONTEXT"] == context and isinstance(loaded["SECURITY_CONTEXT"], SecurityContext)
        assert loaded["n"] == 1 and loaded["_created_at"] == 1.5
        assert await store.exists("sid") and not await store.exists("SID")

        await store.save("sid", {"n": 2}, ttl=60)
        assert await store.get("sid") == {"n": 2}
        await store.delete("sid")
        assert await store.get("sid") is None and not await store.exists("sid")


async def test_expired_sessions_are_not_read_and_are_purged(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with repository_datasources(relational_backend) as datasources:
        store = SqlSessionStore(datasources.registry.primary, clock=lambda: now[0], purge_interval=None)
        await store.save("short", {"a": 1}, ttl=10)
        await store.save("long", {"a": 2}, ttl=600)

        now[0] = now[0] + timedelta(seconds=11)

        assert await store.get("short") is None and not await store.exists("short")
        assert await store.purge_expired() == 1
        async with datasources.engine.connect() as connection:
            remaining = (await connection.execute(select(sessions.c.session_id))).scalars().all()
        assert remaining == ["long"]


async def test_session_writes_purge_expired_sessions(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with repository_datasources(relational_backend) as datasources:
        store = SqlSessionStore(datasources.registry.primary, clock=lambda: now[0], purge_interval=timedelta(0))
        for index in range(3):
            await store.save(f"old-{index}", {}, ttl=1)
        now[0] = now[0] + timedelta(seconds=5)

        await store.save("new", {}, ttl=60)

        async with datasources.engine.connect() as connection:
            count = (await connection.execute(select(func.count()).select_from(sessions))).scalar_one()
        assert count == 1


async def test_without_ddl_the_missing_tables_fail_fast(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    with pytest.raises(FrameworkSchemaError, match="pyfly_sessions"):
        await SqlSessionStore(engine, create_table=False).start()
    with pytest.raises(FrameworkSchemaError, match="pyfly_session_registrations"):
        await PostgresSessionRegistry(engine, create_table=False).start()
