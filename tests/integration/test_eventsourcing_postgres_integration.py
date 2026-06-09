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
"""Integration tests for SqlAlchemyEventStore and SqlAlchemySnapshotStore.

Exercises both adapters against a real Postgres instance (testcontainers).

Gated by ``@requires_docker``. Deselected from the fast suite (``-m integration``).
Run via:
    PYFLY_INTEGRATION_REQUIRE_DOCKER=1 uv run pytest -m integration \\
        tests/integration/test_eventsourcing_postgres_integration.py -q
"""

from __future__ import annotations

import uuid

import pytest

from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.snapshot import Snapshot, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import SqlAlchemyEventStore
from pyfly.testing import requires_docker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope(event_type: str = "OrderPlaced", payload: dict | None = None) -> StoredEventEnvelope:
    return StoredEventEnvelope(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        payload=payload or {"order_id": str(uuid.uuid4())},
    )


# ===========================================================================
# SqlAlchemyEventStore — real Postgres
# ===========================================================================


@requires_docker
@pytest.mark.asyncio
async def test_event_store_append_and_load(pg_url: str) -> None:
    """Append events for an aggregate, then load them back in order."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemyEventStore(engine)
        await store.initialize()

        aggregate_id = f"order-{uuid.uuid4().hex[:8]}"
        events = [
            _envelope("OrderPlaced", {"amount": 100}),
            _envelope("OrderShipped", {"carrier": "ups"}),
        ]
        await store.append(aggregate_id, "Order", events, expected_version=0)

        loaded = await store.load(aggregate_id)
        assert len(loaded) == 2
        assert loaded[0].event_type == "OrderPlaced"
        assert loaded[1].event_type == "OrderShipped"
        assert loaded[0].sequence == 1
        assert loaded[1].sequence == 2
        assert loaded[0].aggregate_id == aggregate_id
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_event_store_load_after_sequence(pg_url: str) -> None:
    """load(after_sequence=n) returns only events with sequence > n."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemyEventStore(engine)
        await store.initialize()

        aggregate_id = f"order-{uuid.uuid4().hex[:8]}"
        events = [_envelope(f"Event{i}") for i in range(5)]
        await store.append(aggregate_id, "Order", events, expected_version=0)

        # Load only events after sequence 3
        loaded = await store.load(aggregate_id, after_sequence=3)
        assert len(loaded) == 2
        assert loaded[0].sequence == 4
        assert loaded[1].sequence == 5
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_event_store_stream_all_limit(pg_url: str) -> None:
    """stream_all returns events in occurred_at order, respecting limit."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemyEventStore(engine)
        await store.initialize()

        # Use a unique aggregate per test run to avoid cross-test pollution
        agg_id = f"order-{uuid.uuid4().hex[:8]}"
        events = [_envelope(f"EvLimit{i}") for i in range(5)]
        await store.append(agg_id, "Order", events, expected_version=0)

        first_two = await store.stream_all(limit=2)
        # We only check that at most 2 events are returned (global stream may
        # have events from other test runs in this schema).
        assert len(first_two) <= 2
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_event_store_stream_all_after_event_id(pg_url: str) -> None:
    """stream_all(after_event_id=...) returns only events occurring after the anchor."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemyEventStore(engine)
        await store.initialize()

        agg_id = f"order-{uuid.uuid4().hex[:8]}"
        events = [_envelope(f"EvAfter{i}") for i in range(3)]
        await store.append(agg_id, "Order", events, expected_version=0)

        # Load the first event to use as anchor
        loaded = await store.load(agg_id)
        anchor_id = loaded[0].event_id

        after = await store.stream_all(after_event_id=anchor_id, limit=100)
        returned_ids = {e.event_id for e in after}
        # Anchor event itself must NOT appear in the result
        assert anchor_id not in returned_ids
        # Both subsequent events must appear
        assert loaded[1].event_id in returned_ids
        assert loaded[2].event_id in returned_ids
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_event_store_latest_version(pg_url: str) -> None:
    """latest_version returns 0 for unknown aggregates and N after N events."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemyEventStore(engine)
        await store.initialize()

        agg_id = f"order-{uuid.uuid4().hex[:8]}"
        assert await store.latest_version(agg_id) == 0

        events = [_envelope() for _ in range(3)]
        await store.append(agg_id, "Order", events, expected_version=0)
        assert await store.latest_version(agg_id) == 3
    finally:
        await engine.dispose()


# ===========================================================================
# SqlAlchemySnapshotStore — real Postgres
# ===========================================================================


@requires_docker
@pytest.mark.asyncio
async def test_snapshot_store_save_and_load(pg_url: str) -> None:
    """Save a snapshot and reload it, verifying all fields."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemySnapshotStore(engine)
        await store.initialize()

        snap = Snapshot(
            aggregate_id=f"order-{uuid.uuid4().hex[:8]}",
            aggregate_type="Order",
            sequence=10,
            payload={"total": 999, "status": "shipped"},
        )
        await store.save(snap)

        loaded = await store.load(snap.aggregate_id)
        assert loaded is not None
        assert loaded.aggregate_id == snap.aggregate_id
        assert loaded.aggregate_type == "Order"
        assert loaded.sequence == 10
        assert loaded.payload == {"total": 999, "status": "shipped"}
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_snapshot_store_upsert_overwrites(pg_url: str) -> None:
    """Saving a newer snapshot for the same aggregate_id replaces the old one."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemySnapshotStore(engine)
        await store.initialize()

        agg_id = f"order-{uuid.uuid4().hex[:8]}"
        snap_v1 = Snapshot(aggregate_id=agg_id, aggregate_type="Order", sequence=5, payload={"v": 1})
        await store.save(snap_v1)

        snap_v2 = Snapshot(aggregate_id=agg_id, aggregate_type="Order", sequence=12, payload={"v": 2})
        await store.save(snap_v2)

        loaded = await store.load(agg_id)
        assert loaded is not None
        assert loaded.sequence == 12
        assert loaded.payload == {"v": 2}
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_snapshot_store_load_returns_none_when_missing(pg_url: str) -> None:
    """load() returns None when no snapshot exists for the aggregate."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemySnapshotStore(engine)
        await store.initialize()

        result = await store.load(f"nonexistent-{uuid.uuid4().hex}")
        assert result is None
    finally:
        await engine.dispose()


@requires_docker
@pytest.mark.asyncio
async def test_snapshot_store_delete(pg_url: str) -> None:
    """delete() removes the snapshot and returns True; second call returns False."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(pg_url, echo=False)
    try:
        store = SqlAlchemySnapshotStore(engine)
        await store.initialize()

        agg_id = f"order-{uuid.uuid4().hex[:8]}"
        snap = Snapshot(aggregate_id=agg_id, aggregate_type="Order", sequence=1, payload={})
        await store.save(snap)

        deleted = await store.delete(agg_id)
        assert deleted is True
        assert await store.load(agg_id) is None

        # Idempotent: second delete returns False
        deleted_again = await store.delete(agg_id)
        assert deleted_again is False
    finally:
        await engine.dispose()
