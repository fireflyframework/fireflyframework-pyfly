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
"""``EventSourcingAutoConfiguration``: the SQL stores run on the context's datasources.

The event store and the snapshot store built their datasource with ``DataSourceRegistry.for_config(config)``,
beside an application's own registry bean, and had no ``datasource`` key. They now resolve the context's
registry, take ``<prefix>.datasource`` or ``<prefix>.url``, create their tables at start when
``pyfly.data.relational.ddl-auto`` allows it, and a projection checkpoint store is wired next to them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Column, MetaData, String, Table, func, insert, select

from pyfly.container.container import Container
from pyfly.container.exceptions import BeanCreationException
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.data.relational.datasource_registry import DataSourceConfigurationError, DataSourceRegistry
from pyfly.data.transaction import infrastructure_unit, transactional
from pyfly.eventsourcing.auto_configuration import EventSourcingAutoConfiguration
from pyfly.eventsourcing.checkpoint import CheckpointStore, InMemoryCheckpointStore, SqlAlchemyCheckpointStore
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.snapshot import SnapshotStore, SqlAlchemySnapshotStore
from pyfly.eventsourcing.store import EventStore, InMemoryEventStore, SqlAlchemyEventStore


def _sqlite(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _config(tmp_path: Path, eventsourcing: dict[str, Any], **relational: Any) -> Config:
    # The relational beans stay off (ddl-auto=create would create every model of the test run); the datasource
    # registry and the transaction managers the stores run on are there regardless.
    return Config(
        {
            "pyfly": {
                "eventsourcing": {"enabled": "true", **eventsourcing},
                "data": {"relational": {"enabled": "false", "url": _sqlite(tmp_path / "app.db"), **relational}},
            }
        }
    )


_SQL = {
    "store": {"provider": "sqlalchemy"},
    "snapshot": {"provider": "sqlalchemy"},
}


async def test_the_stores_run_on_the_context_s_registry_bean(tmp_path: Path) -> None:
    config = Config({"pyfly": {"eventsourcing": {**_SQL, "projection": {"checkpoint": {"provider": "sqlalchemy"}}}}})
    own = DataSourceRegistry(Config({"pyfly": {"data": {"relational": {"url": _sqlite(tmp_path / "own.db")}}}}))
    container = Container()
    container.register_instance(DataSourceRegistry, own)
    auto = EventSourcingAutoConfiguration()
    try:
        store = auto.event_store(config, container)
        snapshots = auto.snapshot_store(config, container)
        checkpoints = auto.projection_checkpoint_store(config, container)
        assert isinstance(store, SqlAlchemyEventStore)
        assert isinstance(snapshots, SqlAlchemySnapshotStore)
        assert isinstance(checkpoints, SqlAlchemyCheckpointStore)
        assert store.engine is own.primary.engine
        assert snapshots.engine is own.primary.engine
        assert checkpoints.engine is own.primary.engine
    finally:
        await own.close()


async def test_the_datasource_keys_name_where_each_store_lives(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {
            "store": {"provider": "sqlalchemy", "datasource": "events"},
            "snapshot": {"provider": "sqlalchemy", "datasource": "events"},
            "projection": {"checkpoint": {"provider": "sqlalchemy", "datasource": "views"}},
        },
        datasources={"events": {"url": _sqlite(tmp_path / "events.db")}, "views": {"url": _sqlite(tmp_path / "v.db")}},
    )
    registry = DataSourceRegistry.for_config(config)
    auto = EventSourcingAutoConfiguration()
    try:
        store = auto.event_store(config, Container())
        assert isinstance(store, SqlAlchemyEventStore) and store.engine is registry.get("events").engine
        snapshots = auto.snapshot_store(config, Container())
        assert isinstance(snapshots, SqlAlchemySnapshotStore) and snapshots.engine is registry.get("events").engine
        checkpoints = auto.projection_checkpoint_store(config, Container())
        assert isinstance(checkpoints, SqlAlchemyCheckpointStore) and checkpoints.engine is registry.get("views").engine
    finally:
        await registry.close()


async def test_a_datasource_and_a_url_together_are_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, {"store": {"provider": "sqlalchemy", "datasource": "primary", "url": "sqlite://"}})
    registry = DataSourceRegistry.for_config(config)
    try:
        with pytest.raises(DataSourceConfigurationError, match=r"pyfly\.eventsourcing\.store\.datasource"):
            EventSourcingAutoConfiguration().event_store(config, Container())
    finally:
        await registry.close()


async def test_the_checkpoints_follow_the_event_store_s_provider_unless_configured(tmp_path: Path) -> None:
    auto = EventSourcingAutoConfiguration()
    assert isinstance(auto.projection_checkpoint_store(Config({}), Container()), InMemoryCheckpointStore)
    memory = _config(tmp_path, {**_SQL, "projection": {"checkpoint": {"provider": "memory"}}})
    registry = DataSourceRegistry.for_config(memory)
    try:
        assert isinstance(auto.projection_checkpoint_store(memory, Container()), InMemoryCheckpointStore)
        following = _config(tmp_path, _SQL)
        assert isinstance(auto.projection_checkpoint_store(following, Container()), SqlAlchemyCheckpointStore)
    finally:
        await registry.close()
        await DataSourceRegistry.for_config(_config(tmp_path, _SQL)).close()
    with pytest.raises(ValueError, match="redis"):
        auto.projection_checkpoint_store(
            Config({"pyfly": {"eventsourcing": {"projection": {"checkpoint": {"provider": "redis"}}}}}), Container()
        )


async def test_the_position_strategy_key_reaches_the_store(tmp_path: Path) -> None:
    config = _config(tmp_path, {"store": {"provider": "sqlalchemy", "position-strategy": "head-row"}})
    registry = DataSourceRegistry.for_config(config)
    try:
        store = EventSourcingAutoConfiguration().event_store(config, Container())
        assert isinstance(store, SqlAlchemyEventStore)
        await store.start()
        assert store.position_strategy == "head-row"
    finally:
        await registry.close()


class _Orders:
    table = Table("wp08_ctx_orders", MetaData(), Column("id", String(32), primary_key=True))


async def test_the_context_starts_the_stores_and_they_join_transactional(tmp_path: Path) -> None:
    """Proof p10 through the application context: an order and its events roll back together."""
    context = ApplicationContext(
        _config(
            tmp_path,
            {**_SQL, "projection": {"checkpoint": {"provider": "sqlalchemy"}}},
            **{"ddl-auto": "create"},
        )
    )
    await context.start()
    try:
        store = context.get_bean(EventStore)
        assert isinstance(store, SqlAlchemyEventStore)
        assert isinstance(context.get_bean(SnapshotStore), SqlAlchemySnapshotStore)
        assert isinstance(context.get_bean(CheckpointStore), SqlAlchemyCheckpointStore)
        async with store.engine.begin() as connection:
            await connection.run_sync(_Orders.table.create)

        @transactional
        async def place(order_id: str, *, fail: bool) -> None:
            async with infrastructure_unit() as session:
                await session.execute(insert(_Orders.table).values(id=order_id))
            await store.append(order_id, "Order", [StoredEventEnvelope(event_type="OrderPlaced")], expected_version=0)
            if fail:
                raise ValueError("payment declined")

        with pytest.raises(ValueError):
            await place("order-1", fail=True)
        await place("order-2", fail=False)

        async with store.engine.connect() as connection:
            placed = (await connection.execute(select(func.count()).select_from(_Orders.table))).scalar()
        assert placed == 1
        assert [event.aggregate_id for event in await store.stream_all()] == ["order-2"]
    finally:
        await context.stop()


async def test_with_migrations_owning_the_schema_a_missing_table_stops_the_context(tmp_path: Path) -> None:
    context = ApplicationContext(_config(tmp_path, _SQL, **{"ddl-auto": "none"}))
    with pytest.raises(BeanCreationException, match="table pyfly_event_store does not exist"):
        await context.start()


def test_memory_stays_the_default() -> None:
    auto = EventSourcingAutoConfiguration()
    assert isinstance(auto.event_store(Config({})), InMemoryEventStore)
