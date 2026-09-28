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
"""A data test's rollback transaction beside the framework's stores and consumers, on every relational lane.

The integration of the data-test rollback (WP11) with the event store and projections (WP08), the outbox
(WP09), the session and token stores (WP10b) and detached work (WP13):

- the SQL session store joins the unit it finds, and the session registry and the OAuth2 token store open
  units of their own (in production they never join a caller's unit): inside a rollback test all of them run
  in the test's transaction, see what the test wrote, and leave nothing behind;
- the outbox relay and a projection runner are ``CONSUMER_PHASE`` beans: they start with the context, before
  the test's transaction begins, so they keep running on the datasource's own connections, never see what the
  test writes, never refuse the test's units, and let the context stop;
- a relay round the test runs itself takes part: it delivers what the test published, inside the test's
  transaction;
- work that runs detached (WP13: an ``@async_method`` call, a saga's steps) takes part when the test awaits it
  where no unit is open, and is refused, as its failure, when a unit of the test that started it is still open;
- a TCC's participants are not detached: started inside a unit of the test, they join it and roll back with
  the test.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, Table, func, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.bean import bean
from pyfly.container.stereotypes import component, configuration, repository, service
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import (
    event_store,
    event_store_head,
    locks,
    oauth2_grants,
    oauth2_token_families,
    projection_checkpoints,
    session_principals,
    session_registrations,
    sessions,
    snapshots,
)
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import IllegalTransactionStateError, transactional
from pyfly.eda.decorators import event_listener
from pyfly.eda.outbox import OutboxTables
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import EventEnvelope
from pyfly.eventsourcing.checkpoint import CheckpointStore
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.projection import FunctionProjection, ProjectionRunner
from pyfly.eventsourcing.store import EventStore, SqlAlchemyEventStore
from pyfly.scheduling.async_methods import AsyncUncaughtExceptionHandler
from pyfly.scheduling.decorators import async_method
from pyfly.security.adapters.postgres_token_store import PostgresTokenStore
from pyfly.security.oauth2.authorization_server import REFRESH_TOKEN, GrantOutcome, TokenRecord
from pyfly.session.concurrency import SessionConcurrencyController
from pyfly.session.ports.outbound import SessionStore
from pyfly.testing import data_slice
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from pyfly.transactional.tcc.annotations import cancel_method, confirm_method, tcc, tcc_participant, try_method
from pyfly.transactional.tcc.engine.tcc_engine import TccEngine
from tests.support.backend_matrix import RelationalBackend

# The context stops within this many seconds after a test, background consumers included.
_STOP_BOUND = 15.0


async def _committed(backend: RelationalBackend, *tables: Table) -> dict[str, int]:
    """The rows another connection sees in each of *tables*: only what was committed."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return {
                table.name: int((await connection.execute(select(func.count()).select_from(table))).scalar() or 0)
                for table in tables
            }
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------------------------
# The session store, the session registry and the OAuth2 token store (WP10b)
# ---------------------------------------------------------------------------------------------------------------

_SESSION_TABLES = (sessions, session_registrations, session_principals, oauth2_grants, oauth2_token_families)


async def test_the_session_and_token_stores_write_inside_the_test_and_leave_nothing(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(*_SESSION_TABLES)  # ddl-auto none: as a migration would
    config = relational_backend.config(
        {
            "pyfly.session.enabled": "true",
            "pyfly.session.store": "postgres",
            "pyfly.session.concurrency.enabled": "true",
            "pyfly.session.concurrency.registry": "postgres",
            "pyfly.session.concurrency.max-sessions": "1",
        }
    )
    async with await data_slice(config=config, rollback=True) as context:
        store = context.get_bean(SessionStore)
        registry = context.get_bean(SessionConcurrencyController).registry
        tokens = PostgresTokenStore(context.get_bean(DataSourceRegistry).primary)
        now = int(time.time())

        await store.save("session-1", {"user": "alice"}, 600)
        await store.save("session-2", {"user": "alice"}, 600)
        first = await registry.register_limited("alice", "session-1", now, max_sessions=1, evict_oldest=True)
        second = await registry.register_limited("alice", "session-2", now + 1, max_sessions=1, evict_oldest=True)
        assert (first.accepted, first.evicted) == (True, ())
        assert (second.accepted, second.evicted) == (True, ("session-1",))
        assert await store.get("session-2") == {"user": "alice"}

        await tokens.issue(TokenRecord("rt-1", REFRESH_TOKEN, "client", now + 600, family_id="family-1"))
        rotated = TokenRecord("rt-2", REFRESH_TOKEN, "client", now + 600, family_id="family-1")
        assert await tokens.rotate("rt-1", rotated, now=now) is GrantOutcome.GRANTED
        assert await tokens.rotate("rt-1", rotated, now=now) is GrantOutcome.REPLAYED  # single use, in the test too
        assert (await tokens.load(REFRESH_TOKEN, "rt-2")) is None  # the replay revoked the family

        assert set((await _committed(relational_backend, *_SESSION_TABLES)).values()) == {0}
    assert set((await _committed(relational_backend, *_SESSION_TABLES)).values()) == {0}


# ---------------------------------------------------------------------------------------------------------------
# The outbox relay (WP09)
# ---------------------------------------------------------------------------------------------------------------


@service
class _Orders:
    def __init__(self, bus: EventPublisher) -> None:
        self._bus = bus

    @transactional
    async def place(self, order_id: int) -> None:
        await self._bus.publish("orders", "order.placed", {"id": order_id})


@service
class _OrderEvents:
    def __init__(self) -> None:
        self.seen: list[Any] = []

    @event_listener(["order.*"])
    async def on_order(self, envelope: EventEnvelope) -> None:
        self.seen.append(envelope.payload["id"])


async def test_a_relay_round_the_test_runs_delivers_what_it_published_and_nothing_stays(
    relational_backend: RelationalBackend,
) -> None:
    outbox = OutboxTables.named()
    await relational_backend.create_tables(*outbox.all())
    config = relational_backend.config(
        {
            "pyfly.eda.provider": "database",
            "pyfly.eda.destinations": "orders",
            "pyfly.eda.group": "wp11-orders",
            "pyfly.eda.outbox.poll-interval": "0.05",
        }
    )
    async with await data_slice(_Orders, _OrderEvents, config=config, rollback=True) as context:
        bus = context.get_bean(EventPublisher)
        seen = context.get_bean(_OrderEvents).seen
        await context.get_bean(_Orders).place(1)  # the commit wakes the background relay
        await asyncio.sleep(0.3)
        assert bus.relay.alive  # type: ignore[attr-defined]
        assert seen == []  # the relay the context started does not see what the test wrote
        assert await bus.relay.run_once() == 1  # type: ignore[attr-defined]
        assert seen == [1]
        assert (await _committed(relational_backend, outbox.events))[outbox.events.name] == 0
        started = time.monotonic()
    assert time.monotonic() - started < _STOP_BOUND
    assert (await _committed(relational_backend, outbox.events, outbox.deliveries)) == {
        outbox.events.name: 0,
        outbox.deliveries.name: 0,
    }


# ---------------------------------------------------------------------------------------------------------------
# A projection runner the context starts (WP08)
# ---------------------------------------------------------------------------------------------------------------

_PROJECTED: list[str] = []


@configuration
class _Projections:
    @bean
    def totals_runner(self, event_store: EventStore, projection_checkpoint_store: CheckpointStore) -> ProjectionRunner:
        async def collect(event: StoredEventEnvelope) -> None:
            _PROJECTED.append(event.event_id)

        return ProjectionRunner(
            FunctionProjection("wp11-totals", collect),
            event_store,
            checkpoints=projection_checkpoint_store,
            poll_interval_s=0.05,
        )


async def _append_elsewhere(backend: RelationalBackend, aggregate_id: str) -> None:
    """Commit an event as another process would: on an engine of its own, in a task that takes no part in the
    test's transaction."""

    async def append() -> None:
        engine = create_async_engine(backend.url, poolclass=NullPool)
        other = SqlAlchemyEventStore(engine, create_table=False)
        try:
            await other.append(aggregate_id, "Account", [_opened()], expected_version=0)
        finally:
            await engine.dispose()

    await asyncio.get_running_loop().create_task(append(), context=contextvars.Context())


def _opened() -> StoredEventEnvelope:
    return StoredEventEnvelope(event_type="Opened", payload={})


async def _wait_for(condition: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_a_projection_runner_of_the_context_never_sees_the_tests_events(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(event_store, event_store_head, snapshots, projection_checkpoints, locks)
    config = relational_backend.config(
        {
            "pyfly.eventsourcing.enabled": "true",
            "pyfly.eventsourcing.store.provider": "sqlalchemy",
            "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
        }
    )
    _PROJECTED.clear()
    async with await data_slice(_Projections, config=config, rollback=True) as context:
        await _append_elsewhere(relational_backend, "account-0")
        await _wait_for(lambda: len(_PROJECTED) == 1)  # the runner is live: it projects what is committed
        store = context.get_bean(EventStore)
        await store.append("account-1", "Account", [_opened()], expected_version=0)
        await store.append("account-2", "Account", [_opened()], expected_version=0)
        assert [event.event_type for event in await store.load("account-1")] == ["Opened"]
        await asyncio.sleep(0.5)  # a few polls of the runner
        assert len(_PROJECTED) == 1  # the test's events are not committed: the runner does not see them
        assert (await _committed(relational_backend, event_store))[event_store.name] == 1
        started = time.monotonic()
    assert time.monotonic() - started < _STOP_BOUND
    assert (await _committed(relational_backend, event_store))[event_store.name] == 1


# ---------------------------------------------------------------------------------------------------------------
# Work that runs detached (WP13): an @async_method call, a saga's steps
# ---------------------------------------------------------------------------------------------------------------


class _AuditRow(Base):
    __tablename__ = "w4_rollback_audit"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))


@repository
class _AuditRows(Repository[_AuditRow, int]):
    pass


@service
class _Auditor:
    def __init__(self, rows: _AuditRows) -> None:
        self.rows = rows

    @async_method
    @transactional
    async def audit(self, kind: str) -> str:
        await self.rows.save(_AuditRow(kind=kind))
        return kind


@service
class _Checkout:
    def __init__(self, rows: _AuditRows, auditor: _Auditor, sagas: SagaEngine, tccs: TccEngine) -> None:
        self.rows = rows
        self.auditor = auditor
        self.sagas = sagas
        self.tccs = tccs
        self.audits: list[asyncio.Task[Any]] = []

    @transactional
    async def place(self) -> None:
        await self.rows.save(_AuditRow(kind="order"))
        self.audits.append(await self.auditor.audit("audit"))

    @transactional
    async def place_through_a_saga(self) -> Any:
        await self.rows.save(_AuditRow(kind="order"))
        return await self.sagas.execute("w4-rollback-saga")

    @transactional
    async def place_through_a_tcc(self) -> Any:
        await self.rows.save(_AuditRow(kind="order"))
        return await self.tccs.execute("w4-rollback-tcc")


@saga(name="w4-rollback-saga")
class _AuditSaga:
    def __init__(self, rows: _AuditRows) -> None:
        self.rows = rows

    @saga_step(id="record", compensate="undo")
    async def record(self) -> None:
        await self.rows.save(_AuditRow(kind="step"))

    async def undo(self) -> None:
        await self.rows.save(_AuditRow(kind="undo"))


@tcc(name="w4-rollback-tcc")
class _AuditTcc:
    def __init__(self, rows: _AuditRows) -> None:
        self.rows = rows

    @tcc_participant(id="audit", order=1)
    class Audit:
        @try_method()
        async def reserve(self) -> None:
            await self.rows.save(_AuditRow(kind="try"))

        @confirm_method()
        async def confirm(self) -> None:
            await self.rows.save(_AuditRow(kind="confirm"))

        @cancel_method()
        async def cancel(self) -> None:
            await self.rows.save(_AuditRow(kind="cancel"))


@component
class _UncaughtErrors(AsyncUncaughtExceptionHandler):
    def __init__(self) -> None:
        self.seen: list[BaseException] = []

    def handle_uncaught_exception(
        self, error: BaseException, method: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.seen.append(error)


def _detached_slice(backend: RelationalBackend) -> Any:
    config = backend.config({"pyfly.transactional.enabled": "true"})
    return data_slice(
        _AuditRows, _Auditor, _Checkout, _AuditSaga, _AuditTcc, _UncaughtErrors, config=config, rollback=True
    )


async def _kinds(context: Any) -> list[str]:
    return sorted(row.kind for row in await context.get_bean(_AuditRows).find_all())


async def test_detached_work_awaited_where_no_unit_is_open_takes_part_and_rolls_back(
    relational_backend: RelationalBackend,
) -> None:
    await relational_backend.create_tables(_AuditRow)
    async with await _detached_slice(relational_backend) as context:
        assert await (await context.get_bean(_Auditor).audit("audit")) == "audit"
        assert (await context.get_bean(SagaEngine).execute("w4-rollback-saga")).success is True
        assert await _kinds(context) == ["audit", "step"]
        assert (await _committed(relational_backend, _AuditRow.__table__))["w4_rollback_audit"] == 0
    assert (await _committed(relational_backend, _AuditRow.__table__))["w4_rollback_audit"] == 0


async def test_detached_work_started_inside_a_unit_of_the_test_is_refused(
    relational_backend: RelationalBackend,
) -> None:
    """In production it would commit on its own connection; the test's one connection cannot run it beside the
    unit that started it, so it fails, loudly, instead of nesting in that unit or hanging."""
    await relational_backend.create_tables(_AuditRow)
    async with await _detached_slice(relational_backend) as context:
        checkout = context.get_bean(_Checkout)
        await checkout.place()
        with pytest.raises(IllegalTransactionStateError, match="would overlap"):
            await checkout.audits[0]
        assert [type(error) for error in context.get_bean(_UncaughtErrors).seen] == [IllegalTransactionStateError]

        result = await checkout.place_through_a_saga()
        assert result.success is False
        assert isinstance(result.error, IllegalTransactionStateError)
        assert await _kinds(context) == ["order", "order"]


async def test_a_tcc_started_inside_a_unit_of_the_test_joins_it_and_rolls_back(
    relational_backend: RelationalBackend,
) -> None:
    """TCC participants run in the caller's task (they are not detached): nothing is refused."""
    await relational_backend.create_tables(_AuditRow)
    async with await _detached_slice(relational_backend) as context:
        result = await context.get_bean(_Checkout).place_through_a_tcc()
        assert result.success is True
        assert await _kinds(context) == ["confirm", "order", "try"]
    assert (await _committed(relational_backend, _AuditRow.__table__))["w4_rollback_audit"] == 0
