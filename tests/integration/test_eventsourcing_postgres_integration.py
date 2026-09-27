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
"""``SqlAlchemyEventStore`` and ``SqlAlchemySnapshotStore`` on PostgreSQL only.

What every backend does is in ``test_event_store_matrix.py``, ``test_snapshot_store_matrix.py`` and
``test_projection_matrix.py``. Here: the ``xid8`` strategy's trade-off (a transaction left open on the server
holds the stream back, and nothing is skipped when it ends), and upgrading the tables an earlier release created
(``TIMESTAMP WITHOUT TIME ZONE`` columns, no global position) with the migration the event-sourcing guide gives.

Run via:
    PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \\
        tests/integration/test_eventsourcing_postgres_integration.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from pyfly.data.relational.framework_schema import FrameworkSchemaError
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.snapshot import Snapshot, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import SqlAlchemyEventStore
from tests.support.backend_matrix import PG, RelationalBackend

pytestmark = pytest.mark.backends(PG)

# The tables as release 26.09.07 created them.
_EARLIER_EVENT_STORE = """
CREATE TABLE IF NOT EXISTS pyfly_event_store (
    event_id        VARCHAR(64) PRIMARY KEY,
    aggregate_id    VARCHAR(64) NOT NULL,
    aggregate_type  VARCHAR(255) NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      VARCHAR(255) NOT NULL,
    payload         TEXT NOT NULL,
    metadata        TEXT NOT NULL,
    occurred_at     TIMESTAMP NOT NULL,
    version         INTEGER NOT NULL,
    tenant_id       VARCHAR(64) NULL,
    UNIQUE (aggregate_id, sequence)
)
"""
_EARLIER_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS pyfly_snapshots (
    aggregate_id   VARCHAR(64) PRIMARY KEY,
    aggregate_type VARCHAR(255) NOT NULL,
    sequence       INTEGER NOT NULL,
    payload        TEXT NOT NULL,
    created_at     TIMESTAMP NOT NULL
)
"""
# The upgrade, as docs/modules/eventsourcing.md gives it for PostgreSQL.
_UPGRADE = [
    "ALTER TABLE pyfly_event_store ADD COLUMN recorded_at TIMESTAMP WITH TIME ZONE NULL",
    "ALTER TABLE pyfly_event_store ADD COLUMN global_position BIGINT NULL",
    "ALTER TABLE pyfly_event_store ALTER COLUMN occurred_at TYPE TIMESTAMP WITH TIME ZONE "
    "USING occurred_at AT TIME ZONE 'UTC'",
    "ALTER TABLE pyfly_snapshots ALTER COLUMN created_at TYPE TIMESTAMP WITH TIME ZONE "
    "USING created_at AT TIME ZONE 'UTC'",
]


def _envelope(event_type: str) -> StoredEventEnvelope:
    return StoredEventEnvelope(event_type=event_type)


async def test_a_transaction_left_open_holds_the_xid8_stream_back_and_nothing_is_skipped(
    relational_backend: RelationalBackend,
) -> None:
    store = SqlAlchemyEventStore(relational_backend.create_engine())
    await store.start()
    assert store.position_strategy == "xid8"
    await store.append("before", "Order", [_envelope("Before")], expected_version=0)

    idle = relational_backend.create_engine()
    async with idle.connect() as open_transaction:
        await open_transaction.execute(text("SELECT pg_current_xact_id()"))  # a transaction with an id, left open
        await store.append("during", "Order", [_envelope("During")], expected_version=0)
        seen = await store.stream_all()
        assert [event.event_type for event in seen] == ["Before"]  # "During" waits for the open transaction
        assert await store.last_position() == seen[-1].global_position
        await open_transaction.rollback()

    later = await store.stream_all(after_position=seen[-1].global_position)
    assert [event.event_type for event in later] == ["During"]


async def test_the_tables_of_an_earlier_release_are_refused_until_migrated_then_their_events_are_placed(
    relational_backend: RelationalBackend,
) -> None:
    engine = relational_backend.create_engine()
    async with engine.begin() as connection:
        await connection.execute(text(_EARLIER_EVENT_STORE))
        await connection.execute(text(_EARLIER_SNAPSHOTS))
        base = datetime(2026, 1, 1, 12, 0)
        for index, name in ((2, "Third"), (0, "First"), (1, "Second")):
            envelope = StoredEventEnvelope(event_type=name, aggregate_id=f"old-{index}", aggregate_type="Order")
            envelope.sequence = 1
            envelope.occurred_at = (base + timedelta(minutes=index)).replace(tzinfo=UTC)
            await connection.execute(
                text(
                    "INSERT INTO pyfly_event_store (event_id, aggregate_id, aggregate_type, sequence, event_type, "
                    "payload, metadata, occurred_at, version, tenant_id) VALUES (:eid, :aid, 'Order', 1, :etype, "
                    ":payload, '{}', :occurred, 1, NULL)"
                ),
                {
                    "eid": envelope.event_id,
                    "aid": envelope.aggregate_id,
                    "etype": name,
                    "payload": envelope.to_json(),
                    "occurred": base + timedelta(minutes=index),
                },
            )
        await connection.execute(
            text(
                "INSERT INTO pyfly_snapshots (aggregate_id, aggregate_type, sequence, payload, created_at) "
                "VALUES ('old-0', 'Order', 1, '{\"total\": 5}', :created)"
            ),
            {"created": base},
        )

    store = SqlAlchemyEventStore(engine)
    with pytest.raises(FrameworkSchemaError, match="pyfly_event_store.global_position does not exist"):
        await store.start()
    snapshots = SqlAlchemySnapshotStore(engine)
    with pytest.raises(FrameworkSchemaError, match="TIMESTAMP WITHOUT TIME ZONE"):
        await snapshots.start()

    async with engine.begin() as connection:
        for statement in _UPGRADE:
            await connection.execute(text(statement))
    await store.start()
    await snapshots.start()

    await store.append("new", "Order", [_envelope("New")], expected_version=0)
    events = await store.stream_all()
    assert [event.event_type for event in events] == ["First", "Second", "Third", "New"]
    assert events[0].occurred_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert await snapshots.load("old-0") == Snapshot("old-0", "Order", 1, {"total": 5})
    await snapshots.save(Snapshot("old-0", "Order", 2, {"total": 7}))
    assert await snapshots.load("old-0") == Snapshot("old-0", "Order", 2, {"total": 7})
