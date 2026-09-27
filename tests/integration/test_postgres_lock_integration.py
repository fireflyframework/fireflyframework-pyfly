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
"""The ``@scheduled`` cluster locks against real databases.

- :class:`LeaseLock`, the default database lock, on every relational lane: a ShedLock-style lease row that
  honors its TTL and holds no connection while the job runs (F10, C150).
- :class:`PostgresAdvisoryLock`, the opt-in PostgreSQL accelerator: its lock connection is idle, not idle in
  transaction, so a server idle timeout never drops it mid-job (F10); a watchdog ends it at the TTL (C150),
  and the tick it ended does not release the lock the next tick took since; an unlock that fails discards the
  connection instead of returning a session that still holds the lock to the pool (C175).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.data.relational.framework_schema import FrameworkSchemaError, locks
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from pyfly.scheduling.adapters.postgres_lock import PostgresAdvisoryLock
from pyfly.scheduling.decorators import scheduled
from pyfly.scheduling.task_scheduler import TaskScheduler
from tests.support.backend_matrix import PG, SERVER_LANES, RelationalBackend

# ---------------------------------------------------------------------------------------------------------
# LeaseLock — every relational lane
# ---------------------------------------------------------------------------------------------------------


async def _lease(backend: RelationalBackend, engine: AsyncEngine | None = None, **options: Any) -> LeaseLock:
    lock = LeaseLock(engine or backend.create_engine(), **options)
    await lock.start()
    return lock


async def test_one_node_holds_the_lease_until_it_releases_it(relational_backend: RelationalBackend) -> None:
    node_a, node_b = await _lease(relational_backend), await _lease(relational_backend)

    assert await node_a.try_acquire("nightly", 30.0) is True
    assert await node_b.try_acquire("nightly", 30.0) is False
    assert await node_a.try_acquire("nightly", 30.0) is False  # a lease is not re-entrant
    await node_a.release("nightly")
    assert await node_b.try_acquire("nightly", 30.0) is True
    await node_b.release("nightly")
    await node_b.release("nightly")  # releasing a lease it no longer holds is a no-op


async def test_a_lease_ends_at_its_ttl_when_the_holder_hangs(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    """C150: the advisory lock ignored ``ttl``: a hung job blocked the job on every node until a restart."""
    node_a, node_b = await _lease(relational_backend), await _lease(relational_backend)

    assert await node_a.try_acquire("nightly", 0.3) is True
    assert await node_b.try_acquire("nightly", 30.0) is False
    await asyncio.sleep(0.5)
    assert await node_b.try_acquire("nightly", 30.0) is True

    with caplog.at_level(logging.WARNING, logger="pyfly.scheduling.adapters.lease_lock"):
        await node_a.release("nightly")  # the hung job finally ends: it must not release node B's lease
    assert "scheduler_lease_expired_before_release" in caplog.text
    assert await node_a.try_acquire("nightly", 30.0) is False


async def test_the_fence_grows_at_every_acquisition(relational_backend: RelationalBackend) -> None:
    node_a, node_b = await _lease(relational_backend), await _lease(relational_backend)

    first = await node_a.acquire("projection", 30.0)
    assert first is not None and first.fence == 1 and first.owner == node_a.owner
    await node_a.release("projection")
    second = await node_b.acquire("projection", 30.0)
    assert second is not None and second.fence == 2 and second.owner == node_b.owner
    holder = await node_a.holder("projection")
    assert holder == second
    assert await node_b.extend("projection", 60.0) is True
    assert await node_a.extend("projection", 60.0) is False  # not its lease


async def test_acquire_waits_for_a_lease_to_come_free(relational_backend: RelationalBackend) -> None:
    node_a, node_b = await _lease(relational_backend), await _lease(relational_backend)
    assert await node_a.try_acquire("schema", 30.0) is True

    async def release_soon() -> None:
        await asyncio.sleep(0.3)
        await node_a.release("schema")

    releaser = asyncio.create_task(release_soon())
    assert await node_b.acquire("schema", 30.0, wait=0.05) is None  # gives up before it is free
    lease = await node_b.acquire("schema", 30.0, wait=5.0, poll_interval=0.05)
    await releaser
    assert lease is not None and lease.owner == node_b.owner


async def test_an_acquisition_acquire_cannot_confirm_is_not_kept(relational_backend: RelationalBackend) -> None:
    """``acquire`` recorded the acquisition before confirming it: when the lease had already ended (a ttl
    shorter than the round trip) or the confirmation failed, the instance kept an acquisition it did not
    report, and released or extended it later as its own."""
    ended = await _lease(relational_backend)
    assert await ended.acquire("nightly", 0.0) is None
    assert ended._held == {}

    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        if calls > 1:  # the acquisition's own reading passes; the confirmation's fails
            raise RuntimeError("the confirmation failed")
        return datetime.now(UTC)

    failing = await _lease(relational_backend, clock=clock)
    with pytest.raises(RuntimeError, match="the confirmation failed"):
        await failing.acquire("weekly", 30.0)
    assert failing._held == {}


async def test_exactly_one_of_many_racing_nodes_gets_the_lease(relational_backend: RelationalBackend) -> None:
    nodes = [await _lease(relational_backend) for _ in range(8)]

    fresh = await asyncio.gather(*(node.try_acquire("contended", 30.0) for node in nodes))
    assert fresh.count(True) == 1

    await nodes[fresh.index(True)].release("contended")
    known = await asyncio.gather(*(node.try_acquire("contended", 30.0) for node in nodes))
    assert known.count(True) == 1


async def test_the_lease_holds_no_connection_while_the_job_runs(relational_backend: RelationalBackend) -> None:
    """F10: the advisory lock kept a pooled connection (idle in transaction) for the whole job."""
    engine = relational_backend.create_engine()
    node = await _lease(relational_backend, engine)

    assert await node.try_acquire("nightly", 30.0) is True
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    await node.release("nightly")


async def test_the_lease_commits_on_its_own_whatever_the_caller_s_transaction_does(
    relational_backend: RelationalBackend,
) -> None:
    """A lock taken inside a transaction that rolls back is still taken: the lease never joins a unit. (A
    read-only one here: on SQLite, one writer, a write unit beside the caller's write unit is refused; the
    server lanes take the lease inside a read-write transaction below.)"""
    engine = relational_backend.create_engine()
    node_a = await _lease(relational_backend, engine)
    node_b = await _lease(relational_backend)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine), read_only=True)

    with pytest.raises(RuntimeError, match="the caller's transaction rolls back"):
        async with template.transaction():
            assert await node_a.try_acquire("nightly", 30.0) is True
            raise RuntimeError("the caller's transaction rolls back")

    assert await node_b.try_acquire("nightly", 30.0) is False


@pytest.mark.backends(*SERVER_LANES)
async def test_the_lease_commits_on_its_own_inside_a_read_write_transaction_that_rolls_back(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    node_a = await _lease(relational_backend, engine)
    node_b = await _lease(relational_backend)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError, match="the caller's transaction rolls back"):
        async with template.transaction() as unit:
            assert unit is not None and not unit.read_only
            await unit.resource.execute(select(locks.c.name))  # the caller's transaction has begun
            assert await node_a.try_acquire("nightly", 30.0) is True
            raise RuntimeError("the caller's transaction rolls back")

    assert await node_b.try_acquire("nightly", 30.0) is False
    holder = await node_b.holder("nightly")
    assert holder is not None and holder.owner == node_a.owner


async def test_stop_releases_the_leases_still_held(relational_backend: RelationalBackend) -> None:
    node_a, node_b = await _lease(relational_backend), await _lease(relational_backend)
    assert await node_a.try_acquire("nightly", 300.0) is True

    await node_a.stop()
    await node_a.stop()

    assert await node_b.try_acquire("nightly", 30.0) is True


async def test_a_name_longer_than_the_column_is_refused_instead_of_truncated(
    relational_backend: RelationalBackend,
) -> None:
    """MySQL and MariaDB's ``INSERT IGNORE`` truncated a name longer than ``pyfly_locks.name``: the first node
    "took" a lease on the truncated key that no release or take-over could ever match again, and the job never
    ran again on any node. PostgreSQL failed every tick; SQLite has no limit."""
    engine = relational_backend.create_engine()
    node_a, node_b = await _lease(relational_backend, engine), await _lease(relational_backend)
    too_long = "n" * (LeaseLock.MAX_NAME_LENGTH + 1)

    for attempt in (node_a.try_acquire(too_long, 30.0), node_a.acquire(too_long, 30.0)):
        with pytest.raises(ValueError, match="at most 255 characters"):
            await attempt
    async with engine.connect() as connection:
        assert (await connection.execute(select(locks.c.name))).all() == []

    longest = "n" * LeaseLock.MAX_NAME_LENGTH
    assert await node_a.try_acquire(longest, 30.0) is True
    assert await node_b.try_acquire(longest, 30.0) is False
    await node_a.release(longest)
    assert await node_b.try_acquire(longest, 30.0) is True
    await node_b.release(longest)


async def test_an_owner_whose_acquisitions_would_not_fit_the_column_is_refused(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    with pytest.raises(ValueError, match="at most 246 characters"):
        LeaseLock(engine, owner="o" * (LeaseLock.MAX_OWNER_LENGTH + 1))

    node = await _lease(relational_backend, engine, owner="o" * LeaseLock.MAX_OWNER_LENGTH)
    lease = await node.acquire("nightly", 30.0)
    assert lease is not None and lease.owner == node.owner and len(lease.locked_by) == LeaseLock.MAX_NAME_LENGTH
    await node.release("nightly")


async def test_without_ddl_a_missing_lease_table_fails_fast(relational_backend: RelationalBackend) -> None:
    lock = LeaseLock(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_locks does not exist"):
        await lock.start()


async def test_a_second_node_runs_a_job_whose_holder_hung_once_its_ttl_passes(
    relational_backend: RelationalBackend,
) -> None:
    """C150 end to end, with two schedulers: node A's run hangs; node B runs the job once the lease's ttl has
    passed, instead of never (the advisory lock held its connection, and the lock, until the process died)."""
    release_a = asyncio.Event()
    runs: dict[str, int] = {"a": 0, "b": 0}

    class NodeAJob:
        @scheduled(fixed_rate=timedelta(seconds=30), lock="wp10a-nightly", lock_ttl=timedelta(milliseconds=600))
        async def run(self) -> None:
            runs["a"] += 1
            await release_a.wait()  # an upstream call with no timeout

    class NodeBJob:
        @scheduled(fixed_rate=timedelta(milliseconds=100), lock="wp10a-nightly", lock_ttl=timedelta(seconds=30))
        async def run(self) -> None:
            runs["b"] += 1

    node_a = TaskScheduler(lock=await _lease(relational_backend))
    node_b = TaskScheduler(lock=await _lease(relational_backend))
    node_a.discover([NodeAJob()])
    await node_a.start()
    await asyncio.sleep(0.2)
    node_b.discover([NodeBJob()])
    await node_b.start()
    try:
        await asyncio.sleep(0.2)
        assert runs == {"a": 1, "b": 0}  # A holds the lease: B's ticks are skipped
        await asyncio.sleep(0.6)
        assert runs["a"] == 1 and runs["b"] >= 1  # the lease ended at its ttl: B took it and runs the job
    finally:
        release_a.set()
        await node_a.stop()
        await node_b.stop()


@pytest.mark.backends(PG)
async def test_taking_a_known_lease_is_one_statement_and_one_round_trip(relational_backend: RelationalBackend) -> None:
    engine = relational_backend.create_engine()
    wire: list[str] = []

    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.driver_connection.add_query_logger(lambda record: wire.append(record.query))

    node = await _lease(relational_backend, engine)
    assert await node.try_acquire("nightly", 30.0) is True
    await node.release("nightly")
    statements: list[str] = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    wire.clear()

    assert await node.try_acquire("nightly", 30.0) is True
    await node.release("nightly")

    # asyncpg's query logger sees BEGIN/COMMIT/ROLLBACK, not prepared statements: the cursor event counts those.
    assert [statement.split()[0].upper() for statement in statements] == ["UPDATE", "UPDATE"]
    assert not [query for query in wire if query.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]


# ---------------------------------------------------------------------------------------------------------
# PostgresAdvisoryLock — the opt-in PostgreSQL accelerator
# ---------------------------------------------------------------------------------------------------------


@pytest.fixture
async def admin(relational_backend: RelationalBackend) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(relational_backend.url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _eventually_free(admin: AsyncEngine, name: str) -> list[int]:
    """The lock's holders once the server has ended a closed session (it does so right after the client
    disconnects, not synchronously), polled for up to two seconds."""
    for _ in range(40):
        holders = await _lock_holders(admin, name)
        if not holders:
            return holders
        await asyncio.sleep(0.05)
    return holders


async def _eventually_held(admin: AsyncEngine, name: str) -> list[int]:
    """The lock's holders once somebody holds it, polled for up to two seconds."""
    for _ in range(40):
        holders = await _lock_holders(admin, name)
        if holders:
            return holders
        await asyncio.sleep(0.05)
    return holders


async def _lock_holders(admin: AsyncEngine, name: str) -> list[int]:
    key = PostgresAdvisoryLock._key(name)
    async with admin.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND ((classid::bigint << 32) | objid::bigint) = (:key)::bigint"
            ),
            {"key": key},
        )
        return [int(pid) for pid in rows.scalars()]


@pytest.mark.backends(PG)
async def test_the_advisory_lock_connection_survives_an_idle_in_transaction_timeout(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """F10 (proof p11): the lock connection sat idle in transaction for the whole job, so a server
    ``idle_in_transaction_session_timeout`` dropped it (and the lock) mid-job and a second node ran it."""
    database = make_url(relational_backend.url).database
    async with admin.connect() as connection:
        await connection.execute(text(f"ALTER DATABASE \"{database}\" SET idle_in_transaction_session_timeout = '1s'"))
    node_a = PostgresAdvisoryLock(relational_backend.create_engine())
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())

    assert await node_a.try_acquire("nightly", 60.0) is True
    async with admin.connect() as connection:
        state = (
            await connection.execute(
                text(
                    "SELECT state FROM pg_stat_activity WHERE datname = :db AND pid <> pg_backend_pid() "
                    "AND query LIKE 'SELECT pg_try_advisory_lock%'"
                ),
                {"db": database},
            )
        ).scalar_one()
    assert state == "idle"
    await asyncio.sleep(2.5)
    assert await node_b.try_acquire("nightly", 60.0) is False
    await node_a.release("nightly")
    assert await node_b.try_acquire("nightly", 60.0) is True
    await node_b.stop()


@pytest.mark.backends(PG)
async def test_the_advisory_watchdog_ends_a_hung_holder_s_lock_at_its_ttl(
    relational_backend: RelationalBackend, admin: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """C150: ``ttl`` was never read; a hung job kept the lock on every node until a restart, logged at DEBUG."""
    node_a = PostgresAdvisoryLock(relational_backend.create_engine())
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())

    with caplog.at_level(logging.WARNING, logger="pyfly.scheduling.adapters.postgres_lock"):
        assert await node_a.try_acquire("nightly", 0.4) is True
        assert await node_b.try_acquire("nightly", 60.0) is False
        await asyncio.sleep(0.8)
        assert await node_b.try_acquire("nightly", 60.0) is True
    assert "scheduler_advisory_lock_expired" in caplog.text
    await node_a.release("nightly")  # the hung job ends: nothing left to release, node B keeps the lock
    assert await _lock_holders(admin, "nightly") != []
    await node_b.release("nightly")
    assert await _eventually_free(admin, "nightly") == []


@pytest.mark.backends(PG)
async def test_a_late_release_keeps_the_lock_the_next_tick_of_the_process_took_since(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """The DistributedLock contract: a release by a holder whose lock already ended must not end a lock another
    holder took since. Two ticks of one job in one process: the watchdog ends the first one's lock at its ttl,
    the second takes it, then the first ends and releases late. That release ended the second tick's lock, and
    another node ran the job beside it."""
    node_a = PostgresAdvisoryLock(relational_backend.create_engine())
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())
    first_lock_ended, first_may_end = asyncio.Event(), asyncio.Event()
    second_holds, second_may_end = asyncio.Event(), asyncio.Event()

    async def first_tick() -> None:
        assert await node_a.try_acquire("nightly", 0.3) is True
        await first_may_end.wait()  # hangs past its ttl
        await node_a.release("nightly")

    async def second_tick() -> None:
        await first_lock_ended.wait()
        assert await node_a.try_acquire("nightly", 60.0) is True
        second_holds.set()
        await second_may_end.wait()
        await node_a.release("nightly")

    first, second = asyncio.create_task(first_tick()), asyncio.create_task(second_tick())
    try:
        await asyncio.sleep(0.3)
        assert await _eventually_free(admin, "nightly") == []  # the watchdog ended the first tick's lock
        first_lock_ended.set()
        await second_holds.wait()
        first_may_end.set()
        await first

        assert await node_b.try_acquire("nightly", 60.0) is False  # the second tick still holds it
        assert await _lock_holders(admin, "nightly") != []
        second_may_end.set()
        await second
        assert await _eventually_free(admin, "nightly") == []  # the second tick's own release ends it
    finally:
        first_may_end.set()
        second_may_end.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await node_b.stop()
        await node_a.stop()


@pytest.mark.backends(PG)
async def test_a_holder_whose_session_ended_does_not_release_the_lock_taken_since(
    relational_backend: RelationalBackend, admin: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """The server ends the session holding the lock (a terminated backend, a restart) while the job runs:
    another run of the job in this process takes the lock, and the first run's late release leaves it alone.
    The first run's hold was overwritten: its connection stayed checked out, and its release ended the second
    run's lock."""
    engine = relational_backend.create_engine(pool_size=2, max_overflow=0)
    node_a = PostgresAdvisoryLock(engine)
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())
    first_may_end, second_holds, second_may_end = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first_run() -> None:
        assert await node_a.try_acquire("nightly", 60.0) is True
        await first_may_end.wait()
        await node_a.release("nightly")

    async def second_run() -> None:
        assert await node_a.try_acquire("nightly", 60.0) is True
        second_holds.set()
        await second_may_end.wait()
        await node_a.release("nightly")

    first = asyncio.create_task(first_run())
    second: asyncio.Task[None] | None = None
    try:
        [pid] = await _eventually_held(admin, "nightly")
        async with admin.connect() as connection:
            await connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        assert await _eventually_free(admin, "nightly") == []

        with caplog.at_level(logging.WARNING, logger="pyfly.scheduling.adapters.postgres_lock"):
            second = asyncio.create_task(second_run())
            await second_holds.wait()
        first_may_end.set()
        await first

        assert await node_b.try_acquire("nightly", 60.0) is False  # the second run still holds it
        await asyncio.sleep(0.05)  # the lost connection is discarded in the background
        assert engine.pool.checkedout() == 1  # type: ignore[attr-defined]  # only the second run's
        second_may_end.set()
        await second
        assert await _eventually_free(admin, "nightly") == []
        assert "scheduler_advisory_lock_lost" in caplog.text
    finally:
        first_may_end.set()
        second_may_end.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        await node_b.stop()
        await node_a.stop()


@pytest.mark.backends(PG)
async def test_a_release_from_a_task_that_never_held_the_lock_ends_it(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """Only a displaced holder's release is a no-op: a task that neither holds the lock nor lost it to the
    watchdog (a shutdown hook, a job that hands its release to another task) still ends it."""
    node = PostgresAdvisoryLock(relational_backend.create_engine())
    acquired = asyncio.create_task(node.try_acquire("nightly", 60.0))
    assert await acquired is True

    await node.release("nightly")

    assert await _eventually_free(admin, "nightly") == []
    assert node._held == {}


@pytest.mark.backends(PG)
async def test_a_hung_tick_does_not_let_another_node_run_beside_the_next_tick(
    relational_backend: RelationalBackend,
) -> None:
    """End to end with two schedulers: node A's first tick hangs past ``lock_ttl``, the watchdog ends its lock,
    and A's second tick takes it. When the first tick ends, its release ended the second tick's lock and node
    B ran the job while that tick still ran within its ttl."""
    runs = {"a": 0, "b": 0}
    release_first, release_second = asyncio.Event(), asyncio.Event()

    class NodeAJob:
        @scheduled(fixed_rate=timedelta(seconds=1), lock="wp10a-advisory", lock_ttl=timedelta(milliseconds=600))
        async def run(self) -> None:
            runs["a"] += 1
            await (release_first if runs["a"] == 1 else release_second).wait()

    class NodeBJob:
        @scheduled(fixed_rate=timedelta(milliseconds=50), lock="wp10a-advisory", lock_ttl=timedelta(seconds=30))
        async def run(self) -> None:
            runs["b"] += 1

    node_a = TaskScheduler(lock=PostgresAdvisoryLock(relational_backend.create_engine()))
    node_b = TaskScheduler(lock=PostgresAdvisoryLock(relational_backend.create_engine()))
    node_a.discover([NodeAJob()])
    node_b.discover([NodeBJob()])
    await node_a.start()
    try:
        await asyncio.sleep(1.2)  # tick 1's lock ended at 0.6 s; tick 2 took it at 1 s, until 1.6 s
        assert runs["a"] == 2
        release_first.set()  # tick 1 ends and releases late
        await asyncio.sleep(0.05)
        await node_b.start()
        await asyncio.sleep(0.15)
        assert runs["b"] == 0, f"node B ran while node A's second tick held the lock within its ttl: {runs}"
    finally:
        release_first.set()
        release_second.set()
        await node_a.stop()
        await node_b.stop()


@pytest.mark.backends(PG)
async def test_a_failed_unlock_discards_the_connection_holding_the_lock(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """C175: an unlock that failed with an error that is not a disconnect returned the connection, and the
    session lock with it, to the pool: nobody else could take the lock, and this node re-entered it."""
    role = f"wp10a_lock_{uuid.uuid4().hex[:8]}"
    url = make_url(relational_backend.url)
    async with admin.connect() as connection:
        await connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw'"))
        await connection.execute(text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {role}'))
        # A real error on a live connection: this role may take advisory locks but not release them.
        await connection.execute(text("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_advisory_unlock(bigint) FROM PUBLIC"))
    limited = create_async_engine(url.set(username=role, password="pw"), pool_size=1, max_overflow=0)
    try:
        node_a = PostgresAdvisoryLock(limited)
        node_b = PostgresAdvisoryLock(relational_backend.create_engine())
        assert await node_a.try_acquire("nightly", 60.0) is True

        with pytest.raises(Exception, match="permission denied"):
            await node_a.release("nightly")

        assert await _eventually_free(admin, "nightly") == []  # the session holding it is gone
        assert await node_b.try_acquire("nightly", 60.0) is True
        async with limited.connect() as connection:  # the pool opened a fresh connection
            assert (await connection.execute(select(1))).scalar_one() == 1
        await node_b.release("nightly")
    finally:
        await limited.dispose()
        async with admin.connect() as connection:
            await connection.execute(text("GRANT EXECUTE ON FUNCTION pg_catalog.pg_advisory_unlock(bigint) TO PUBLIC"))
            await connection.execute(text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {role}'))
            await connection.execute(text(f"DROP ROLE {role}"))


@pytest.mark.backends(PG)
async def test_a_failed_acquisition_discards_its_connection_and_any_lock_it_took(
    relational_backend: RelationalBackend, admin: AsyncEngine
) -> None:
    """``try_acquire`` returned its connection to the pool after an error. When the server had granted the lock
    and only the reply was lost (an error after it, a cancellation), the session lock went back to the pool
    with it, taken for good (C175's failure mode on the acquiring side)."""
    async with admin.connect() as connection:
        # A real function earlier on the search path than pg_catalog's: it takes the lock, then fails.
        await connection.execute(text("CREATE SCHEMA wp10a_trap"))
        await connection.execute(
            text(
                "CREATE FUNCTION wp10a_trap.pg_try_advisory_lock(key bigint) RETURNS boolean LANGUAGE plpgsql AS "
                "$$ BEGIN PERFORM pg_catalog.pg_try_advisory_lock(key); "
                "RAISE EXCEPTION 'the reply was lost'; END $$"
            )
        )
    trapped = relational_backend.create_engine(
        pool_size=1, max_overflow=0, connect_args={"server_settings": {"search_path": "wp10a_trap, pg_catalog"}}
    )
    node_a = PostgresAdvisoryLock(trapped)
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())

    with pytest.raises(Exception, match="the reply was lost"):
        await node_a.try_acquire("nightly", 60.0)

    assert trapped.pool.checkedout() == 0  # type: ignore[attr-defined]
    assert await _eventually_free(admin, "nightly") == []  # the session that took it is gone
    assert await node_b.try_acquire("nightly", 60.0) is True
    await node_b.release("nightly")


@pytest.mark.backends(PG)
async def test_the_advisory_lock_stop_releases_what_it_holds(relational_backend: RelationalBackend) -> None:
    node_a = PostgresAdvisoryLock(relational_backend.create_engine())
    node_b = PostgresAdvisoryLock(relational_backend.create_engine())
    assert await node_a.try_acquire("nightly", 60.0) is True

    await node_a.stop()
    await node_a.stop()

    assert await node_b.try_acquire("nightly", 60.0) is True
    await node_b.release("nightly")


def test_the_lease_table_is_the_framework_s() -> None:
    assert locks.name == "pyfly_locks"


# ---------------------------------------------------------------------------------------------------------
# Wired by the application context
# ---------------------------------------------------------------------------------------------------------


async def test_the_context_wires_the_lease_lock_and_creates_its_table(relational_backend: RelationalBackend) -> None:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.scheduling.lock import DistributedLock

    ctx = ApplicationContext(
        relational_backend.config(
            {
                "pyfly.data.relational.enabled": "false",  # ddl-auto=create would create every model of the run
                "pyfly.data.relational.ddl-auto": "create",
                "pyfly.scheduling.lock.provider": "database",
            }
        )
    )
    await ctx.start()
    try:
        lock = ctx.get_bean(DistributedLock)  # type: ignore[type-abstract]
        assert isinstance(lock, LeaseLock)
        assert await lock.try_acquire("wp10a-context", 60.0) is True
    finally:
        await ctx.stop()  # stop releases the lease it still holds
    assert await (await _lease(relational_backend)).try_acquire("wp10a-context", 60.0) is True
