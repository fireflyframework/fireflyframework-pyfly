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
"""Integration tests for durable orchestration persistence providers.

Exercises :class:`RedisPersistenceProvider` against a real Redis (testcontainers), and
:class:`SqlAlchemyPersistenceProvider` on every relational lane of the backend matrix (SQLite file in the
fast suite; PostgreSQL, MySQL and MariaDB with ``-m integration``), alone and wired by the application
context, where the saga and TCC engines persist through it too.

Run the server lanes via:
    PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \\
        tests/integration/test_orchestration_persistence_integration.py -q
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.container.exceptions import BeanCreationException
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.framework_schema import FrameworkSchemaError, orchestration_state
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.testing import requires_docker
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import ExecutionPersistenceProvider, ExecutionState
from pyfly.transactional.core.recovery import RecoveryService
from pyfly.transactional.persistence.sqlalchemy_adapter import SqlAlchemyPersistenceProvider
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.saga.persistence.recovery import SagaRecoveryService
from pyfly.transactional.shared.ports.outbound import TransactionalPersistencePort
from pyfly.transactional.tcc.annotations import confirm_method, tcc, tcc_participant, try_method
from pyfly.transactional.tcc.core.context import TccContext
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from tests.support.backend_matrix import PG, RelationalBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    pattern: ExecutionPattern = ExecutionPattern.SAGA,
    minutes_ago: int = 0,
    completed: bool = False,
) -> ExecutionState:
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return ExecutionState(
        correlation_id=str(uuid.uuid4()),
        name="integration-test",
        pattern=pattern,
        status=status,
        started_at=now,
        updated_at=now,
        completed_at=now if completed else None,
        payload={
            "correlation_id": str(uuid.uuid4()),
            "name": "integration-test",
            "pattern": pattern.value,
            "status": status.value,
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "completed_at": None,
            "input": None,
            "headers": {},
            "dry_run": False,
            "tcc_phase": None,
            "error": None,
            "steps": {},
            "variables": {},
            "idempotency_keys": [],
            "try_results": {},
        },
    )


# ===========================================================================
# RedisPersistenceProvider — real Redis
# ===========================================================================


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_save_find(redis_url: str) -> None:
    """Save → find round-trip against a real Redis instance."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=f"test:{uuid.uuid4().hex[:8]}:")
        state = _make_state()
        await provider.save(state)

        found = await provider.find(state.correlation_id)
        assert found is not None
        assert found.correlation_id == state.correlation_id
        assert found.status == state.status
        assert found.pattern == state.pattern
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_find_all(redis_url: str) -> None:
    """find_all enumerates all saved states for a given prefix."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    prefix = f"test:{uuid.uuid4().hex[:8]}:"
    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=prefix)
        s1 = _make_state(status=ExecutionStatus.RUNNING)
        s2 = _make_state(status=ExecutionStatus.COMPLETED, completed=True)
        await provider.save(s1)
        await provider.save(s2)

        all_states = await provider.find_all()
        ids = {s.correlation_id for s in all_states}
        assert s1.correlation_id in ids
        assert s2.correlation_id in ids

        running_only = await provider.find_all(status=ExecutionStatus.RUNNING)
        assert all(s.status == ExecutionStatus.RUNNING for s in running_only)
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_delete(redis_url: str) -> None:
    """delete removes the key from Redis."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    prefix = f"test:{uuid.uuid4().hex[:8]}:"
    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client, key_prefix=prefix)
        state = _make_state()
        await provider.save(state)

        deleted = await provider.delete(state.correlation_id)
        assert deleted is True
        assert await provider.find(state.correlation_id) is None

        # Idempotent: deleting again returns False
        deleted_again = await provider.delete(state.correlation_id)
        assert deleted_again is False
    finally:
        await client.aclose()


@requires_docker
@pytest.mark.asyncio
async def test_redis_persistence_is_healthy(redis_url: str) -> None:
    """is_healthy returns True against a live Redis server."""
    import redis.asyncio as aioredis

    from pyfly.transactional.persistence.redis_adapter import RedisPersistenceProvider

    client = aioredis.from_url(redis_url)
    try:
        provider = RedisPersistenceProvider(client)
        assert await provider.is_healthy() is True
    finally:
        await client.aclose()


# ===========================================================================
# SqlAlchemyPersistenceProvider — every relational lane
# ===========================================================================


async def _sql_provider(backend: RelationalBackend, **options: Any) -> SqlAlchemyPersistenceProvider:
    provider = SqlAlchemyPersistenceProvider(backend.create_engine(), **options)
    await provider.start()
    return provider


def _with(state: ExecutionState, **changes: Any) -> ExecutionState:
    return dataclasses.replace(state, **changes)


async def test_sql_provider_round_trips_and_upserts_on_every_backend(relational_backend: RelationalBackend) -> None:
    """C069: the upsert was ``ON CONFLICT`` in ``text()``, a syntax error on MySQL and MariaDB. F12: the
    provider creates its table when it starts (nothing ever called ``initialize()``)."""
    provider = await _sql_provider(relational_backend)
    running = _make_state(status=ExecutionStatus.RUNNING)
    workflow = _make_state(pattern=ExecutionPattern.WORKFLOW)
    now = datetime.now(UTC)

    await provider.save(running)
    await provider.save(_with(running, status=ExecutionStatus.COMPLETED, updated_at=now, completed_at=now))
    await provider.save(workflow)

    found = await provider.find(running.correlation_id)
    assert found is not None and found.status is ExecutionStatus.COMPLETED
    assert {s.correlation_id for s in await provider.find_all()} == {running.correlation_id, workflow.correlation_id}
    completed = await provider.find_all(status=ExecutionStatus.COMPLETED)
    assert [s.correlation_id for s in completed] == [running.correlation_id]
    workflows = await provider.find_all(pattern=ExecutionPattern.WORKFLOW)
    assert [s.correlation_id for s in workflows] == [workflow.correlation_id]
    assert await provider.find("never-saved") is None
    assert await provider.delete(workflow.correlation_id) is True
    assert await provider.delete(workflow.correlation_id) is False
    assert await provider.is_healthy() is True


async def test_find_stale_and_cleanup_compare_instants_on_every_backend(relational_backend: RelationalBackend) -> None:
    """C080: ``find_stale``/``cleanup`` bound aware datetimes to ``TIMESTAMP`` columns (asyncpg ``DataError`` on
    every recovery scan, so stuck executions were never reported and the table grew without bound)."""
    provider = await _sql_provider(relational_backend)
    stale = _make_state(minutes_ago=120)
    fresh = _make_state(minutes_ago=1)
    old_done = _make_state(status=ExecutionStatus.COMPLETED, minutes_ago=10 * 24 * 60, completed=True)
    recent_done = _make_state(status=ExecutionStatus.FAILED, minutes_ago=5, completed=True)
    for state in (stale, fresh, old_done, recent_done):
        await provider.save(state)
    madrid = timezone(timedelta(hours=2))
    an_hour_ago = (datetime.now(UTC) - timedelta(hours=1)).astimezone(madrid)  # an instant in another zone

    assert [s.correlation_id for s in await provider.find_stale(an_hour_ago)] == [stale.correlation_id]
    assert await provider.cleanup(timedelta(days=7)) == 1
    assert await provider.find(old_done.correlation_id) is None
    assert await provider.find(recent_done.correlation_id) is not None
    assert await provider.find(stale.correlation_id) is not None  # an execution that is not over is kept


async def test_the_columns_hold_utc_instants_with_microseconds(relational_backend: RelationalBackend) -> None:
    provider = await _sql_provider(relational_backend)
    madrid = timezone(timedelta(hours=2))
    started = datetime(2026, 9, 27, 14, 30, 5, 123456, tzinfo=madrid)
    state = _with(_make_state(), started_at=started, updated_at=started + timedelta(seconds=1))
    await provider.save(state)

    async with provider.engine.connect() as connection:
        row = (
            await connection.execute(
                select(orchestration_state.c.started_at, orchestration_state.c.updated_at).where(
                    orchestration_state.c.correlation_id == state.correlation_id
                )
            )
        ).one()
    assert row.started_at == started and row.started_at.tzinfo == UTC and row.started_at.microsecond == 123456
    assert row.updated_at == started + timedelta(seconds=1)


async def test_a_custom_table_name_is_created_and_used(relational_backend: RelationalBackend) -> None:
    """C080: ``initialize()`` created ``pyfly_orchestration_state`` whatever the configured name."""
    provider = await _sql_provider(relational_backend, table_name="wp10a_saga_state")
    state = _make_state()
    await provider.save(state)

    def names(connection: Connection) -> set[str]:
        return set(inspect(connection).get_table_names())

    async with provider.engine.connect() as connection:
        tables = await connection.run_sync(names)
    assert "wp10a_saga_state" in tables and "pyfly_orchestration_state" not in tables
    assert await provider.find(state.correlation_id) is not None


async def test_state_saved_in_a_transaction_commits_or_rolls_back_with_it(
    relational_backend: RelationalBackend,
) -> None:
    """The provider joins the unit of work bound for its datasource: no dual write beside a business step."""
    engine = relational_backend.create_engine()
    provider = SqlAlchemyPersistenceProvider(engine)
    await provider.start()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))
    discarded, kept = _make_state(), _make_state()

    with pytest.raises(RuntimeError, match="the business step failed"):
        async with template.transaction():
            await provider.save(discarded)
            raise RuntimeError("the business step failed")
    async with template.transaction():
        await provider.save(kept)

    assert await provider.find(discarded.correlation_id) is None
    assert await provider.find(kept.correlation_id) is not None


async def test_without_ddl_a_missing_table_fails_fast_at_start(relational_backend: RelationalBackend) -> None:
    provider = SqlAlchemyPersistenceProvider(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_orchestration_state does not exist"):
        await provider.start()


def _log_wire_queries(engine: AsyncEngine, sink: list[str]) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.driver_connection.add_query_logger(lambda record: sink.append(record.query))


@pytest.mark.backends(PG)
async def test_each_operation_is_one_round_trip_on_postgresql(relational_backend: RelationalBackend) -> None:
    """C093: every operation was ``BEGIN`` + statement + ``COMMIT``/``ROLLBACK``, three round trips."""
    engine = relational_backend.create_engine()
    wire: list[str] = []
    _log_wire_queries(engine, wire)
    provider = SqlAlchemyPersistenceProvider(engine)
    await provider.start()
    state = _make_state(minutes_ago=90)
    wire.clear()

    await provider.save(state)
    await provider.find(state.correlation_id)
    await provider.find_stale(datetime.now(UTC))
    await provider.cleanup(timedelta(days=1))
    await provider.delete(state.correlation_id)

    assert not [query for query in wire if query.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]


# ===========================================================================
# Wired by the application context: recovery scan, saga and TCC engines
# ===========================================================================


@saga(name="wp10a-order-saga")
class Wp10aOrderSaga:
    @saga_step(id="reserve")
    def reserve(self) -> str:
        return "reserved"


@tcc(name="wp10a-payment-tcc")
class Wp10aPaymentTcc:
    @tcc_participant(id="credit", order=1)
    class Credit:
        @try_method()
        def do_try(self, ctx: TccContext) -> str:
            return "held"

        @confirm_method()
        def do_confirm(self, ctx: TccContext) -> None:
            return None


async def _context(backend: RelationalBackend, *beans: type, ddl_auto: str = "create") -> ApplicationContext:
    # The relational beans stay off: ddl-auto=create would create every model of the test run (Base.metadata),
    # while the datasource registry and the transaction managers the provider runs on are there regardless.
    ctx = ApplicationContext(
        backend.config(
            {
                "pyfly.data.relational.enabled": "false",
                "pyfly.data.relational.ddl-auto": ddl_auto,
                "pyfly.transactional.enabled": "true",
                "pyfly.transactional.persistence.provider": "sqlalchemy",
            }
        )
    )
    for bean in beans:
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


async def test_the_context_creates_the_state_table_and_the_recovery_scan_reads_it(
    relational_backend: RelationalBackend,
) -> None:
    """F12: with ``provider=sqlalchemy`` every scan failed with ``relation "pyfly_orchestration_state" does not
    exist``: nothing called ``initialize()``."""
    ctx = await _context(relational_backend)
    try:
        recovery = ctx.get_bean(RecoveryService)
        assert await recovery.find_stale() == []
        assert await recovery.cleanup() == 0
        assert isinstance(ctx.get_bean(ExecutionPersistenceProvider), SqlAlchemyPersistenceProvider)
    finally:
        await ctx.stop()


async def test_with_migrations_owning_the_schema_a_missing_table_stops_the_context(
    relational_backend: RelationalBackend,
) -> None:
    """F12's other half: with ``ddl-auto=none`` the provider does not create its table, and a missing one fails
    the start instead of every recovery scan."""
    with pytest.raises(BeanCreationException, match="table pyfly_orchestration_state does not exist"):
        await _context(relational_backend, ddl_auto="none")


async def test_saga_and_tcc_executions_are_persisted_by_the_configured_provider(
    relational_backend: RelationalBackend,
) -> None:
    """C079: the saga and TCC engines always used the in-memory adapter, whatever the provider said."""
    ctx = await _context(relational_backend, Wp10aOrderSaga, Wp10aPaymentTcc)
    try:
        saga_result = await ctx.get_bean(SagaEngine).execute("wp10a-order-saga")
        tcc_result = await ctx.get_bean(TccEngine).execute("wp10a-payment-tcc")
        assert saga_result.success and tcc_result.success

        provider = ctx.get_bean(ExecutionPersistenceProvider)
        saga_state = await provider.find(saga_result.correlation_id)
        tcc_state = await provider.find(tcc_result.correlation_id)
        assert saga_state is not None and tcc_state is not None
        assert (saga_state.pattern, saga_state.status, saga_state.name) == (
            ExecutionPattern.SAGA,
            ExecutionStatus.COMPLETED,
            "wp10a-order-saga",
        )
        assert (tcc_state.pattern, tcc_state.status, tcc_state.name) == (
            ExecutionPattern.TCC,
            ExecutionStatus.COMPLETED,
            "wp10a-payment-tcc",
        )
    finally:
        await ctx.stop()


async def test_a_saga_cut_short_by_a_crash_is_recovered_by_the_next_process(
    relational_backend: RelationalBackend,
) -> None:
    """C079: a pod that dies mid-saga left nothing to recover. Its in-flight state is now in the database, and
    the next process's ``SagaRecoveryService`` finds it and marks it failed."""
    first = await _context(relational_backend)
    try:
        await first.get_bean(TransactionalPersistencePort).persist_state(  # type: ignore[type-abstract]
            {
                "saga_name": "wp10a-order-saga",
                "correlation_id": "wp10a-crashed",
                "headers": {},
                "started_at": datetime.now(UTC) - timedelta(minutes=5),
            }
        )
    finally:
        await first.stop()  # the process dies before the saga completes

    second = await _context(relational_backend)
    try:
        assert await second.get_bean(SagaRecoveryService).recover_stale(stale_threshold_seconds=0) == 1
        port = second.get_bean(TransactionalPersistencePort)  # type: ignore[type-abstract]
        recovered = await port.get_state("wp10a-crashed")
        assert recovered is not None and recovered["status"] == "FAILED" and recovered["successful"] is False
    finally:
        await second.stop()
