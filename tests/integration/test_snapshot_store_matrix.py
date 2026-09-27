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
"""``SqlAlchemySnapshotStore`` and ``EventSourcedRepository`` on every relational lane (C069, F8).

The snapshot store upserted with ``INSERT ... ON CONFLICT`` in ``text()`` (a syntax error on MySQL and
MariaDB), created a ``TIMESTAMP`` column holding naive local times, and wrote on a connection of its own, so a
snapshot outlived the business transaction that rolled its events back. It now uses the framework table
``pyfly_snapshots``, the dialect's own conditional upsert, and joins the ambient unit of work, as the event
store does: an aggregate's events and its snapshot commit or roll back together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pyfly.data.relational.framework_schema import FrameworkSchemaError, snapshots
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.eventsourcing.aggregate import AggregateRoot
from pyfly.eventsourcing.event import DomainEvent
from pyfly.eventsourcing.repository import EventSourcedRepository
from pyfly.eventsourcing.snapshot import Snapshot, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import SqlAlchemyEventStore
from pyfly.testing import StatementCounter
from tests.support.backend_matrix import RelationalBackend


@dataclass
class Deposited(DomainEvent):
    amount: int = 0


class Account(AggregateRoot):
    def __init__(self) -> None:
        super().__init__()
        self.balance = 0
        self.when("Deposited", lambda account, event: setattr(account, "balance", account.balance + event.amount))


async def _snapshots(backend: RelationalBackend, **options: object) -> SqlAlchemySnapshotStore:
    store = SqlAlchemySnapshotStore(backend.create_engine(), **options)  # type: ignore[arg-type]
    await store.start()
    return store


async def test_snapshots_round_trip_and_only_a_newer_one_replaces_the_stored_one(
    relational_backend: RelationalBackend,
) -> None:
    store = await _snapshots(relational_backend)
    await store.save(Snapshot("acc-1", "Account", 5, {"balance": 50, "owner": "zoë"}))
    await store.save(Snapshot("acc-1", "Account", 3, {"balance": 30}))  # older: ignored
    loaded = await store.load("acc-1")
    assert loaded == Snapshot("acc-1", "Account", 5, {"balance": 50, "owner": "zoë"})

    await store.save(Snapshot("acc-1", "Account", 7, {"balance": 70}))
    assert await store.load("acc-1") == Snapshot("acc-1", "Account", 7, {"balance": 70})
    assert await store.load("ACC-1") is None  # keys compare exactly on every backend
    assert await store.delete("acc-1") is True
    assert await store.delete("acc-1") is False
    assert await store.load("acc-1") is None


async def test_the_snapshot_time_is_a_utc_instant(relational_backend: RelationalBackend) -> None:
    store = await _snapshots(relational_backend)
    before = datetime.now(UTC)
    await store.save(Snapshot("acc-2", "Account", 1, {}))
    async with store.engine.connect() as connection:
        created_at = (await connection.execute(select(snapshots.c.created_at))).scalar_one()
    assert created_at.tzinfo is not None
    assert before - timedelta(seconds=5) <= created_at <= datetime.now(UTC) + timedelta(seconds=5)


async def test_a_snapshot_saved_in_a_unit_that_rolls_back_is_not_kept(relational_backend: RelationalBackend) -> None:
    store = await _snapshots(relational_backend)
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(store.engine))

    with pytest.raises(RuntimeError):
        async with template.transaction():
            await store.save(Snapshot("acc-3", "Account", 9, {"balance": 90}))
            raise RuntimeError("the command failed after the snapshot")

    assert await store.load("acc-3") is None


async def test_the_snapshot_table_is_created_at_start_and_only_checked_without_ddl(
    relational_backend: RelationalBackend,
) -> None:
    unchecked = SqlAlchemySnapshotStore(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_snapshots does not exist"):
        await unchecked.start()

    custom = await _snapshots(relational_backend, table_name="wp08_account_snapshots")
    await custom.save(Snapshot("acc-4", "Account", 2, {"balance": 20}))
    reread = SqlAlchemySnapshotStore(custom.engine, table_name="wp08_account_snapshots", create_table=False)
    await reread.start()
    assert await reread.load("acc-4") == Snapshot("acc-4", "Account", 2, {"balance": 20})


async def test_a_snapshot_save_is_one_statement_where_the_backend_has_a_conditional_upsert(
    relational_backend: RelationalBackend,
) -> None:
    store = await _snapshots(relational_backend)
    await store.save(Snapshot("acc-5", "Account", 1, {}))
    with StatementCounter(store.engine) as counter:
        await store.save(Snapshot("acc-5", "Account", 2, {}))
    expected = 1 if relational_backend.dialect in ("postgresql", "sqlite") else 2
    assert counter.count() == expected


async def test_an_aggregate_s_events_and_snapshot_commit_or_roll_back_together(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    events = SqlAlchemyEventStore(engine)
    snapshot_store = SqlAlchemySnapshotStore(engine)
    await events.start()
    await snapshot_store.start()
    repository: EventSourcedRepository[Account] = EventSourcedRepository(
        events, Account, snapshots=snapshot_store, snapshot_interval=2
    )
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    account = Account()
    account.id = "acc-6"
    account.apply(Deposited(10))
    account.apply(Deposited(5))
    with pytest.raises(RuntimeError):
        async with template.transaction():
            await repository.save(account)
            raise RuntimeError("rejected after saving")
    assert await repository.load("acc-6") is None
    assert await snapshot_store.load("acc-6") is None

    fresh = Account()
    fresh.id = "acc-6"
    fresh.apply(Deposited(10))
    fresh.apply(Deposited(5))
    async with template.transaction():
        await repository.save(fresh)
    snapshot = await snapshot_store.load("acc-6")
    assert snapshot is not None and snapshot.sequence == 2 and snapshot.payload == {"balance": 15}
    reloaded = await repository.load("acc-6")
    assert reloaded is not None and reloaded.balance == 15 and reloaded.version == 2
