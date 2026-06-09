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
"""Unit tests for durable eventsourcing adapters (no Docker required).

Covers:
  * SqlAlchemySnapshotStore against sqlite+aiosqlite in-memory
  * EventSourcingPublisher with a fake EventPublisher
  * Provider-selection logic in EventSourcingAutoConfiguration
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pyfly.eda.types import EventEnvelope
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.publisher import EventSourcingPublisher
from pyfly.eventsourcing.snapshot import InMemorySnapshotStore, Snapshot, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import InMemoryEventStore, SqlAlchemyEventStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class FakeEventPublisher:
    """Minimal in-memory EventPublisher for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        pass

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.calls.append(
            {
                "destination": destination,
                "event_type": event_type,
                "payload": payload,
                "headers": headers,
            }
        )

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_sqlite_engine() -> Any:
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)


# ===========================================================================
# SqlAlchemySnapshotStore — sqlite in-memory
# ===========================================================================


class TestSqlAlchemySnapshotStore:
    @pytest.fixture
    async def store(self) -> SqlAlchemySnapshotStore:
        engine = _make_sqlite_engine()
        s = SqlAlchemySnapshotStore(engine)
        await s.initialize()
        return s

    @pytest.mark.asyncio
    async def test_save_and_load_round_trip(self, store: SqlAlchemySnapshotStore) -> None:
        snap = Snapshot(aggregate_id="agg-1", aggregate_type="Order", sequence=5, payload={"total": 99})
        await store.save(snap)
        loaded = await store.load("agg-1")
        assert loaded is not None
        assert loaded.aggregate_id == "agg-1"
        assert loaded.aggregate_type == "Order"
        assert loaded.sequence == 5
        assert loaded.payload == {"total": 99}

    @pytest.mark.asyncio
    async def test_load_returns_none_when_missing(self, store: SqlAlchemySnapshotStore) -> None:
        result = await store.load("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing(self, store: SqlAlchemySnapshotStore) -> None:
        snap_v1 = Snapshot(aggregate_id="agg-2", aggregate_type="Order", sequence=3, payload={"v": 1})
        await store.save(snap_v1)

        snap_v2 = Snapshot(aggregate_id="agg-2", aggregate_type="Order", sequence=7, payload={"v": 2})
        await store.save(snap_v2)

        loaded = await store.load("agg-2")
        assert loaded is not None
        assert loaded.sequence == 7
        assert loaded.payload == {"v": 2}

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_deleted(self, store: SqlAlchemySnapshotStore) -> None:
        snap = Snapshot(aggregate_id="agg-3", aggregate_type="Order", sequence=1, payload={})
        await store.save(snap)
        deleted = await store.delete("agg-3")
        assert deleted is True
        assert await store.load("agg-3") is None

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, store: SqlAlchemySnapshotStore) -> None:
        deleted = await store.delete("does-not-exist")
        assert deleted is False


# ===========================================================================
# EventSourcingPublisher — fake bus
# ===========================================================================


class TestEventSourcingPublisher:
    @pytest.mark.asyncio
    async def test_publish_calls_event_publisher_with_correct_fields(self) -> None:
        fake = FakeEventPublisher()
        publisher = EventSourcingPublisher(fake, destination="my.topic")

        envelope = StoredEventEnvelope(
            event_id="evt-1",
            aggregate_id="agg-1",
            aggregate_type="Order",
            sequence=1,
            event_type="OrderPlaced",
            payload={"order_id": "o-1", "amount": 42},
            metadata={},
            version=1,
        )
        await publisher.publish(envelope)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["destination"] == "my.topic"
        assert call["event_type"] == "OrderPlaced"
        assert call["payload"] == {"order_id": "o-1", "amount": 42}
        headers = call["headers"] or {}
        assert headers["aggregate_id"] == "agg-1"
        assert headers["aggregate_type"] == "Order"
        assert headers["sequence"] == "1"

    @pytest.mark.asyncio
    async def test_publish_uses_default_destination(self) -> None:
        fake = FakeEventPublisher()
        publisher = EventSourcingPublisher(fake)  # default destination

        envelope = StoredEventEnvelope(event_type="SomeEvent", payload={})
        await publisher.publish(envelope)

        assert fake.calls[0]["destination"] == "pyfly.events"

    @pytest.mark.asyncio
    async def test_publish_includes_tenant_id_in_headers(self) -> None:
        fake = FakeEventPublisher()
        publisher = EventSourcingPublisher(fake)

        envelope = StoredEventEnvelope(
            event_type="TenantEvent",
            payload={},
            tenant_id="tenant-42",
        )
        await publisher.publish(envelope)

        headers = fake.calls[0]["headers"] or {}
        assert headers["tenant_id"] == "tenant-42"

    @pytest.mark.asyncio
    async def test_publish_all_sends_all_envelopes(self) -> None:
        fake = FakeEventPublisher()
        publisher = EventSourcingPublisher(fake, destination="events")

        envelopes = [StoredEventEnvelope(event_type=f"Event{i}", payload={"i": i}) for i in range(3)]
        await publisher.publish_all(envelopes)

        assert len(fake.calls) == 3
        assert [c["event_type"] for c in fake.calls] == ["Event0", "Event1", "Event2"]

    @pytest.mark.asyncio
    async def test_publish_merges_string_metadata_as_headers(self) -> None:
        fake = FakeEventPublisher()
        publisher = EventSourcingPublisher(fake)

        envelope = StoredEventEnvelope(
            event_type="MetaEvent",
            payload={},
            metadata={"correlation_id": "corr-1", "non_str": 42},
        )
        await publisher.publish(envelope)

        headers = fake.calls[0]["headers"] or {}
        assert headers["correlation_id"] == "corr-1"
        # Non-string metadata values should not appear in headers
        assert "non_str" not in headers


# ===========================================================================
# Provider-selection unit tests
# ===========================================================================


class TestProviderSelection:
    """Assert correct store/snapshot types per config (mock engine creation)."""

    def _make_config(self, values: dict[str, str]) -> Any:
        cfg = MagicMock()
        cfg.get.side_effect = lambda key, default=None: values.get(key, default)
        return cfg

    @pytest.mark.asyncio
    async def test_event_store_defaults_to_in_memory(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config({})
        store = auto.event_store(cfg)
        assert isinstance(store, InMemoryEventStore)

    @pytest.mark.asyncio
    async def test_event_store_memory_explicit(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config({"pyfly.eventsourcing.store.provider": "memory"})
        store = auto.event_store(cfg)
        assert isinstance(store, InMemoryEventStore)

    @pytest.mark.asyncio
    async def test_event_store_sqlalchemy_provider(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config(
            {
                "pyfly.eventsourcing.store.provider": "sqlalchemy",
                "pyfly.eventsourcing.store.url": "sqlite+aiosqlite:///:memory:",
            }
        )
        fake_engine = MagicMock()
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=fake_engine):
            store = auto.event_store(cfg)
        assert isinstance(store, SqlAlchemyEventStore)

    @pytest.mark.asyncio
    async def test_event_store_invalid_provider_raises(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config({"pyfly.eventsourcing.store.provider": "redis"})
        with pytest.raises(ValueError, match="redis"):
            auto.event_store(cfg)

    @pytest.mark.asyncio
    async def test_snapshot_store_defaults_to_in_memory(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config({})
        store = auto.snapshot_store(cfg)
        assert isinstance(store, InMemorySnapshotStore)

    @pytest.mark.asyncio
    async def test_snapshot_store_sqlalchemy_provider(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config(
            {
                "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
                "pyfly.eventsourcing.snapshot.url": "sqlite+aiosqlite:///:memory:",
            }
        )
        fake_engine = MagicMock()
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=fake_engine):
            store = auto.snapshot_store(cfg)
        assert isinstance(store, SqlAlchemySnapshotStore)

    @pytest.mark.asyncio
    async def test_snapshot_store_falls_back_to_relational_url(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config(
            {
                "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
                "pyfly.data.relational.url": "postgresql+asyncpg://localhost/test",
            }
        )
        fake_engine = MagicMock()
        with patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=fake_engine) as mock_cae:
            auto.snapshot_store(cfg)
        mock_cae.assert_called_once_with("postgresql+asyncpg://localhost/test", echo=False)

    @pytest.mark.asyncio
    async def test_event_sourcing_publisher_absent_when_no_event_publisher(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        cfg = self._make_config({})
        result = auto.event_sourcing_publisher(cfg, event_publisher=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_event_sourcing_publisher_wired_when_event_publisher_present(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        fake = FakeEventPublisher()
        cfg = self._make_config({"pyfly.eventsourcing.eda.destination": "custom.dest"})
        result = auto.event_sourcing_publisher(cfg, event_publisher=fake)
        assert isinstance(result, EventSourcingPublisher)
        assert result._destination == "custom.dest"

    @pytest.mark.asyncio
    async def test_event_sourcing_publisher_uses_default_destination(self) -> None:
        from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration

        auto = EventSourcingAutoConfiguration()
        fake = FakeEventPublisher()
        cfg = self._make_config({})
        result = auto.event_sourcing_publisher(cfg, event_publisher=fake)
        assert isinstance(result, EventSourcingPublisher)
        assert result._destination == "pyfly.events"
