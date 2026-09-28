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
"""Snapshot store SPI + in-memory + SQL adapters.

A snapshot is an aggregate's state as of one of its events (``sequence``); loading the aggregate replays only
the events after it. A store keeps the newest snapshot of each aggregate: saving an older one than it holds
changes nothing.

:class:`SqlAlchemySnapshotStore` keeps them in the framework table ``pyfly_snapshots``, saves with the
dialect's conditional upsert (:mod:`pyfly.data.relational.upsert`), and runs through
:func:`~pyfly.data.transaction.infrastructure_unit`: inside a unit of work on its datasource a snapshot commits or
rolls back with the events it follows (``EventSourcedRepository.save`` writes both in the caller's unit).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.data.transaction import infrastructure_unit, outside_transaction

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass
class Snapshot:
    aggregate_id: str
    aggregate_type: str
    sequence: int
    payload: dict[str, Any]


@runtime_checkable
class SnapshotStore(Protocol):
    async def save(self, snapshot: Snapshot) -> None: ...
    async def load(self, aggregate_id: str) -> Snapshot | None: ...
    async def delete(self, aggregate_id: str) -> bool: ...


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self._store: dict[str, Snapshot] = {}
        self._lock = asyncio.Lock()

    async def save(self, snapshot: Snapshot) -> None:
        async with self._lock:
            existing = self._store.get(snapshot.aggregate_id)
            if existing is None or existing.sequence < snapshot.sequence:
                self._store[snapshot.aggregate_id] = snapshot

    async def load(self, aggregate_id: str) -> Snapshot | None:
        async with self._lock:
            return self._store.get(aggregate_id)

    async def delete(self, aggregate_id: str) -> bool:
        async with self._lock:
            return self._store.pop(aggregate_id, None) is not None


class SqlAlchemySnapshotStore:
    """Async SQL adapter for the snapshot store (see the module documentation).

    *engine* is where the snapshots live: an ``AsyncEngine``, a registry ``DataSource`` or a datasource name.
    *table_name* renames the table (declared on the framework metadata under that name). With *create_table*
    false the store never creates its table and only checks it at :meth:`start` (migrations own the schema).
    """

    def __init__(self, engine: Any, *, table_name: str = "pyfly_snapshots", create_table: bool = True) -> None:
        self._target = engine
        self._table_name = table_name
        self._create_table = create_table
        self._table_object: Table | None = None
        self._single_statement: bool | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the table when allowed, then check it; raises ``FrameworkSchemaError`` when it is unusable.

        The application context starts the store; one built by hand starts on first use."""
        from pyfly.data.relational.framework_schema import ensure_tables

        with outside_transaction():
            await ensure_tables(self._target, self._table, create=self._create_table)
        self._started = True

    async def _ready(self) -> None:
        if not self._started:
            await self.start()

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller)."""

    async def initialize(self) -> None:
        """Create the table if it does not exist (kept for callers that set the store up by hand)."""
        from pyfly.data.relational.framework_schema import ensure_tables

        with outside_transaction():
            await ensure_tables(self._target, self._table, create=True)
        self._started = True

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the store's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import snapshots_table

            self._table_object = snapshots_table(self._table_name)
        return self._table_object

    def _one_statement_save(self) -> bool:
        """Whether a save is one statement (a conditional ``ON CONFLICT`` upsert: PostgreSQL, SQLite), which runs
        on an autocommit connection outside a unit of work where the backend makes that cheaper."""
        if self._single_statement is None:
            from pyfly.data.relational.upsert import backend_name, native_upsert

            self._single_statement = native_upsert(backend_name(self.engine), conditional=True)
        return self._single_statement

    # ------------------------------------------------------------------
    # SnapshotStore
    # ------------------------------------------------------------------

    async def save(self, snapshot: Snapshot) -> None:
        """Store *snapshot*, unless the store holds a snapshot of the aggregate at a later (or the same) sequence."""
        from pyfly.data.relational.upsert import upsert

        await self._ready()
        values = {
            "aggregate_id": snapshot.aggregate_id,
            "aggregate_type": snapshot.aggregate_type,
            "sequence": snapshot.sequence,
            "payload": json.dumps(snapshot.payload),
            "created_at": datetime.now(UTC),
        }
        async with infrastructure_unit(self._target, single_statement=self._one_statement_save()) as session:
            await upsert(
                session,
                self._table,
                values,
                key=["aggregate_id"],
                where=lambda existing, incoming: existing.sequence < incoming.sequence,
            )

    async def load(self, aggregate_id: str) -> Snapshot | None:
        from sqlalchemy import select

        await self._ready()
        table = self._table
        statement = select(table.c.aggregate_type, table.c.sequence, table.c.payload).where(
            table.c.aggregate_id == aggregate_id
        )
        async with infrastructure_unit(self._target, read_only=True) as session:
            row = (await session.execute(statement)).first()
        if row is None:
            return None
        return Snapshot(
            aggregate_id=aggregate_id,
            aggregate_type=str(row.aggregate_type),
            sequence=int(row.sequence),
            payload=json.loads(row.payload),
        )

    async def delete(self, aggregate_id: str) -> bool:
        from sqlalchemy import delete

        await self._ready()
        table = self._table
        async with infrastructure_unit(self._target, single_statement=True) as session:
            result = await session.execute(delete(table).where(table.c.aggregate_id == aggregate_id))
        return int(getattr(result, "rowcount", 0)) > 0
