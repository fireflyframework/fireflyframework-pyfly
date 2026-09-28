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
- A capped login reruns its unit when MariaDB's snapshot isolation reports that a registration it read and
  is evicting changed meanwhile (error 1020): a logout or the purge must not fail a concurrent login.
- Through the whole login request (``SessionFilter`` plus ``OAuth2LoginHandler``) on two replicas, an evicted
  session stayed evicted only if its own login's request did not save it again when the handler returned; it
  did, and concurrent evict-oldest logins left more logged-in sessions in ``pyfly_sessions`` than the cap.
- Any request of a session that changes it and ends after the session was evicted or logged out saved it back
  (an upsert): the revoked session authenticated again. The filter replaces a session the store held only
  while the store still holds it (one conditional UPDATE).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util import await_only

from pyfly.data.relational.framework_schema import FrameworkSchemaError, session_registrations, sessions
from pyfly.kernel.exceptions import OptimisticLockingFailureException
from pyfly.security.context import SecurityContext
from pyfly.session.adapters.postgres_registry import PostgresSessionRegistry
from pyfly.session.adapters.sql_session_store import SqlSessionStore
from pyfly.session.concurrency import ConcurrencyControlPolicy, SessionConcurrencyController, SessionRegistration
from pyfly.testing.statement_counter import statement_verb
from tests.integration import _session_logins as logins
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


@pytest.mark.parametrize("strategy", ["evict-oldest", "reject-new"])
@pytest.mark.parametrize(("max_sessions", "concurrent"), [(1, 8), (2, 16)])
async def test_concurrent_logins_through_the_login_flow_keep_the_cap(
    relational_backend: RelationalBackend, strategy: str, max_sessions: int, concurrent: int
) -> None:
    """Rounds of concurrent OAuth2 logins of one principal through two replicas' session filters and login
    handlers: after each round the logged-in sessions in the shared store are exactly the registered ones."""
    async with _replicas(relational_backend) as replicas:
        flows = [
            logins.Replica(store, _controller(store, registry, max_sessions=max_sessions, strategy=strategy))
            for store, registry, _ in replicas
        ]

        await logins.concurrent_logins_keep_the_cap(
            flows,
            max_sessions=max_sessions,
            evict_oldest=strategy == "evict-oldest",
            concurrent=concurrent,
            rounds=3,
        )


@pytest.mark.parametrize("strategy", ["evict-oldest", "reject-new"])
async def test_a_session_write_after_the_login_keeps_the_cap(
    relational_backend: RelationalBackend, strategy: str
) -> None:
    """The application changes the session after the login handler returned, on two replicas."""
    async with _replicas(relational_backend) as replicas:
        flows = [
            logins.Replica(
                store, _controller(store, registry, max_sessions=2, strategy=strategy), write_after_login=True
            )
            for store, registry, _ in replicas
        ]

        await logins.concurrent_logins_keep_the_cap(
            flows, max_sessions=2, evict_oldest=strategy == "evict-oldest", concurrent=16, rounds=3
        )


@pytest.mark.parametrize("revocation", ["eviction", "logout", "logout-filter"])
async def test_a_revoked_session_stays_revoked(relational_backend: RelationalBackend, revocation: str) -> None:
    async with _replicas(relational_backend, count=1) as [(store, registry, _engine)]:
        replica = logins.Replica(store, _controller(store, registry, max_sessions=1))

        await logins.a_revoked_session_stays_revoked(replica, revocation)


async def test_one_login_at_a_time_through_the_login_flow_keeps_the_latest_sessions(
    relational_backend: RelationalBackend,
) -> None:
    async with _replicas(relational_backend) as replicas:
        flows = [logins.Replica(store, _controller(store, registry, max_sessions=2)) for store, registry, _ in replicas]

        await logins.concurrent_logins_keep_the_cap(flows, max_sessions=2, evict_oldest=True, concurrent=1, rounds=4)


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
# A capped login racing a deregistration or a renewal of the session it evicts
# ---------------------------------------------------------------------------------------------------------

_PAUSED_LOGIN: ContextVar[bool] = ContextVar("wp10b_paused_login", default=False)


@contextlib.contextmanager
def _hold_deletes(engine: AsyncEngine, times: int) -> Iterator[asyncio.Queue[asyncio.Event]]:
    """Hold the task that set ``_PAUSED_LOGIN`` before each of its first *times* DELETEs: each hold puts an
    event on the yielded queue, and the task goes on once that event is set."""
    holds: asyncio.Queue[asyncio.Event] = asyncio.Queue()
    held = 0

    def before(_conn: Connection, _cursor: Any, statement: str, *_args: Any) -> None:
        nonlocal held
        if not _PAUSED_LOGIN.get() or held == times or statement_verb(statement) != "DELETE":
            return
        held += 1
        resume = asyncio.Event()
        holds.put_nowait(resume)
        await_only(resume.wait())

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield holds
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


async def _capped_login(registry: PostgresSessionRegistry) -> SessionRegistration:
    """Alice's third login under a cap of two, evicting the oldest, held at its DELETEs by ``_hold_deletes``."""
    _PAUSED_LOGIN.set(True)
    return await registry.register_limited("alice", "new", 3.0, max_sessions=2, evict_oldest=True)


@pytest.mark.parametrize("racer", ["deregister", "renew"])
async def test_a_capped_login_survives_a_change_to_the_registration_it_evicts(
    relational_backend: RelationalBackend, racer: str
) -> None:
    """A logout, a login's drop of a dead session and the purge change registrations without the principal's
    lock. When a capped login has read the registrations and is about to evict one of them, such a change
    commits first: MariaDB's snapshot isolation then fails the login's DELETE ("Record has changed since last
    read", error 1020), and the login failed with a raw driver error after its session was saved. The login
    reruns its unit instead: it is accepted, and the cap holds."""
    async with _replicas(relational_backend, count=1) as [(_store, registry, engine)]:
        await registry.register_limited("alice", "old", 1.0, max_sessions=5, evict_oldest=True)
        await registry.register_limited("alice", "live", 2.0, max_sessions=5, evict_oldest=True)

        async def change() -> None:
            if racer == "deregister":  # a logout, a login's drop of a dead session, the purge
                await registry.deregister("alice", "old")
            else:  # the purge renewing the registration of a live session
                await registry.renew(["old"])

        with _hold_deletes(engine, times=1) as holds:
            login_task = asyncio.create_task(_capped_login(registry))
            resume = await asyncio.wait_for(holds.get(), 10)
            change_task = asyncio.create_task(change())
            # On a server the change needs no lock the login holds and commits at once; on SQLite it waits
            # for the login's write lock, so the login resumes first.
            await asyncio.wait({change_task}, timeout=1)
            resume.set()
            registration, _ = await asyncio.gather(login_task, change_task)

        assert registration.accepted
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["live", "new"]


@pytest.mark.backends("mariadb")
async def test_a_capped_login_gives_up_after_three_conflicts(relational_backend: RelationalBackend) -> None:
    """The rerun is bounded: a login whose eviction meets a changed registration on every attempt fails with
    the translated OptimisticLockingFailureException (from the driver's error) after three attempts, and
    changes nothing."""
    async with _replicas(relational_backend, count=1) as [(_store, registry, engine)]:
        await registry.register_limited("alice", "old", 1.0, max_sessions=5, evict_oldest=True)
        await registry.register_limited("alice", "live", 2.0, max_sessions=5, evict_oldest=True)

        with _hold_deletes(engine, times=3) as holds:
            login_task = asyncio.create_task(_capped_login(registry))
            for _attempt in range(3):
                resume = await asyncio.wait_for(holds.get(), 10)
                await registry.renew(["old"])
                resume.set()
            with pytest.raises(OptimisticLockingFailureException) as raised:
                await login_task

        assert isinstance(raised.value.__cause__, OperationalError)
        assert [sid for sid, _ in await registry.list_sessions("alice")] == ["old", "live"]


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


async def test_replace_changes_only_a_session_the_store_holds(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with repository_datasources(relational_backend) as datasources:
        store = SqlSessionStore(datasources.registry.primary, clock=lambda: now[0], purge_interval=None)
        assert await store.replace("gone", {"a": 1}, ttl=60) is False
        assert await store.get("gone") is None

        await store.save("sid", {"a": 1}, ttl=10)
        assert await store.replace("sid", {"a": 2}, ttl=60) is True
        assert await store.replace("sid", {"a": 2}, ttl=60) is True  # the same values again still count
        assert await store.get("sid") == {"a": 2}

        now[0] = now[0] + timedelta(seconds=30)
        assert await store.exists("sid")  # the replace moved the expiry 60 seconds on
        now[0] = now[0] + timedelta(seconds=31)
        assert await store.replace("sid", {"a": 3}, ttl=60) is False  # expired: not brought back
        assert await store.get("sid") is None


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
