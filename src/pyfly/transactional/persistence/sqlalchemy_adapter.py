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
"""SQLAlchemy persistence of orchestration state, on any relational backend.

Mirrors the Java engine's R2DBC-based persistence. One row per execution in the framework table
``pyfly_orchestration_state`` (:func:`~pyfly.data.relational.framework_schema.orchestration_state_table`):
the columns the recovery scan and the queries filter on, and the execution's JSON in ``payload``.

- **Portable.** The table is a Core table with portable types (UTC instants with microseconds on every
  backend), and :meth:`SqlAlchemyPersistenceProvider.save` is the dialect's upsert
  (:mod:`pyfly.data.relational.upsert`): ``ON CONFLICT`` on PostgreSQL and SQLite, ``ON DUPLICATE KEY
  UPDATE`` on MySQL and MariaDB, a portable form elsewhere.
- **Created at start.** :meth:`~SqlAlchemyPersistenceProvider.start` (the application context calls it)
  creates the table when the schema strategy allows it and checks it either way, failing fast when it is
  missing (:func:`~pyfly.data.relational.framework_schema.ensure_tables`).
- **Part of the caller's transaction.** Every operation runs through
  :func:`~pyfly.data.transaction.infrastructure_unit`: inside a unit of work on the provider's datasource
  it joins that unit (the state commits or rolls back with the business step), and outside one it gets a
  short unit of its own, which on PostgreSQL is a single statement on an autocommit connection (one round
  trip instead of three).

Designed to fail gracefully (no module-level import of SQLAlchemy) so the engine still starts even if the
optional ``data-relational`` extra is absent.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import infrastructure_unit
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import (
    ExecutionState,
    StateSerializer,
)

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

_TERMINAL = tuple(status.value for status in ExecutionStatus if status.is_terminal)


class SqlAlchemyPersistenceProvider:
    """Durable :class:`~pyfly.transactional.core.persistence.ExecutionPersistenceProvider` on a SQL
    datasource (see the module documentation).

    *engine* is where the state lives: an ``AsyncEngine``, a registry ``DataSource``, or a datasource name.
    *table_name* renames the table (declared on the framework metadata under that name). With
    *create_table* false the provider never creates its table and only checks it at start (migrations own
    the schema).
    """

    def __init__(
        self,
        engine: Any,
        *,
        table_name: str = "pyfly_orchestration_state",
        create_table: bool = True,
    ) -> None:
        self._target = engine
        self._table_name = table_name
        self._create_table = create_table
        self._table_object: Table | None = None
        self._dialect: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the table when allowed, then check it; raises ``FrameworkSchemaError`` when it is unusable."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, self._table, create=self._create_table)

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller)."""

    async def initialize(self) -> None:
        """Create the table if it does not exist (kept for callers that set the provider up by hand)."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, self._table, create=True)

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the provider's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import orchestration_state_table

            self._table_object = orchestration_state_table(self._table_name)
        return self._table_object

    def _single_statement_upsert(self) -> bool:
        if self._dialect is None:
            from pyfly.data.relational.upsert import backend_name

            self._dialect = backend_name(self.engine)
        from pyfly.data.relational.upsert import native_upsert

        return native_upsert(self._dialect)

    # ------------------------------------------------------------------
    # ExecutionPersistenceProvider
    # ------------------------------------------------------------------

    async def save(self, state: ExecutionState) -> None:
        """Insert the execution, or update its status, timestamps and payload (one upsert)."""
        from pyfly.data.relational.upsert import upsert

        values = {
            "correlation_id": state.correlation_id,
            "execution_name": state.name,
            "pattern": state.pattern.value,
            "status": state.status.value,
            "started_at": state.started_at,
            "updated_at": state.updated_at,
            "completed_at": state.completed_at,
            "payload": StateSerializer.serialize(state),
        }
        async with infrastructure_unit(self._target, single_statement=self._single_statement_upsert()) as session:
            await upsert(
                session,
                self._table,
                values,
                key=["correlation_id"],
                update=["status", "updated_at", "completed_at", "payload"],
            )

    async def find(self, correlation_id: str) -> ExecutionState | None:
        from sqlalchemy import select

        table = self._table
        async with infrastructure_unit(self._target, read_only=True) as session:
            raw = (
                await session.execute(select(table.c.payload).where(table.c.correlation_id == correlation_id))
            ).scalar_one_or_none()
        return StateSerializer.deserialize(raw) if raw is not None else None

    async def find_all(
        self,
        *,
        status: ExecutionStatus | None = None,
        pattern: ExecutionPattern | None = None,
    ) -> list[ExecutionState]:
        from sqlalchemy import select

        table = self._table
        statement = select(table.c.payload)
        if status is not None:
            statement = statement.where(table.c.status == status.value)
        if pattern is not None:
            statement = statement.where(table.c.pattern == pattern.value)
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (await session.execute(statement)).scalars().all()
        return [StateSerializer.deserialize(raw) for raw in rows]

    async def find_stale(self, before: datetime) -> list[ExecutionState]:
        """Executions that are not over and were last updated before *before* (an instant, in any zone)."""
        from sqlalchemy import select

        table = self._table
        statement = select(table.c.payload).where(table.c.updated_at < before, table.c.status.not_in(_TERMINAL))
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (await session.execute(statement)).scalars().all()
        return [StateSerializer.deserialize(raw) for raw in rows]

    async def delete(self, correlation_id: str) -> bool:
        from sqlalchemy import delete

        table = self._table
        async with infrastructure_unit(self._target, single_statement=True) as session:
            result = await session.execute(delete(table).where(table.c.correlation_id == correlation_id))
        return bool(result.rowcount > 0)

    async def cleanup(self, older_than: timedelta, *, patterns: Collection[ExecutionPattern] | None = None) -> int:
        """Delete the executions that ended (or were last updated) more than *older_than* ago, only those of
        *patterns* when given (the saga and TCC port cleans up its own executions this way, in one
        statement)."""
        from sqlalchemy import delete, func, literal

        from pyfly.data.relational.framework_schema import UtcTimestamp

        table = self._table
        cutoff = literal(datetime.now(UTC) - older_than, UtcTimestamp())
        statement = delete(table).where(
            table.c.status.in_(_TERMINAL), func.coalesce(table.c.completed_at, table.c.updated_at) < cutoff
        )
        if patterns is not None:
            statement = statement.where(table.c.pattern.in_([pattern.value for pattern in patterns]))
        async with infrastructure_unit(self._target, single_statement=True) as session:
            result = await session.execute(statement)
        return int(result.rowcount)

    async def is_healthy(self) -> bool:
        """Whether the datasource answers (``SELECT 1``, spelled for every dialect: ``FROM DUAL`` on Oracle)."""
        try:
            from sqlalchemy import literal, select

            async with infrastructure_unit(self._target, read_only=True) as session:
                await session.execute(select(literal(1)))
            return True
        except Exception:  # noqa: BLE001
            return False
