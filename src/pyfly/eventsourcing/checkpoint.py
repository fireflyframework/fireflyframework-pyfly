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
"""Projection checkpoints: the global position each projection has applied the event stream up to.

A :class:`~pyfly.eventsourcing.projection.ProjectionRunner` applies the events in batches. Each batch is a
:meth:`CheckpointStore.batch`: it moves the projection's checkpoint from the position the batch started at
(*expected*) to the position of its last event, and the batch's events are applied inside it.

:class:`SqlAlchemyCheckpointStore` keeps the checkpoints in the framework table ``pyfly_projection_checkpoints``
and runs each batch as one unit of work on its datasource:

- the checkpoint moves first, with a conditional ``UPDATE ... WHERE position = :expected``, so the row stays
  locked until the unit ends and a second runner's batch from the same position waits, finds the checkpoint
  moved, and applies nothing (it is not *claimed*);
- the projection's handlers run in that unit: their repository calls, ``@transactional`` methods and
  :func:`~pyfly.data.transaction.infrastructure_unit` calls on the same datasource join it;
- the unit commits the read model's writes and the checkpoint together, or rolls both back.

A read model on the checkpoint's datasource therefore gets every event exactly once, across restarts, failures
and replicas. A handler whose effects live elsewhere (another database, a broker, an email) gets every event
at least once: a batch that fails after such an effect runs again. The store also hands out the lease that makes
one replica the projection's active runner (:meth:`SqlAlchemyCheckpointStore.projection_lease`, a
:class:`~pyfly.scheduling.adapters.lease_lock.LeaseLock` on the same datasource).

:class:`InMemoryCheckpointStore` keeps the positions in the process, for tests and single-process development:
it is not transactional, so a batch that fails keeps what it applied before the failure.

Rebuilding a read model is explicit: stop its runners, clear the read model, :meth:`CheckpointStore.reset` the
checkpoint, start the runners.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.data.transaction import (
    Propagation,
    TransactionTemplate,
    infrastructure_unit,
    outside_transaction,
    resolve_manager,
)

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

    from pyfly.scheduling.adapters.lease_lock import LeaseLock


class CheckpointBatch:
    """One batch of a projection, from its checkpoint's position *expected* to *target*.

    ``claimed`` says whether the batch may apply its events: the checkpoint was at *expected* (it is ``False``
    when another runner moved it meanwhile). The runner calls :meth:`applied` after each event it applied.
    Once the batch has ended, ``position`` is where the checkpoint is as far as this batch knows: *target* when
    the batch committed, *expected* when it rolled back (or, on a store that is not transactional, the last
    position :meth:`applied` recorded).
    """

    __slots__ = ("claimed", "expected", "position", "progress", "projection", "target")

    def __init__(self, projection: str, *, expected: int, target: int) -> None:
        self.projection = projection
        self.expected = expected
        self.target = target
        self.claimed = False
        self.position = expected
        self.progress: int | None = None

    def applied(self, position: int) -> None:
        """Record that the events up to *position* have been applied."""
        self.progress = position

    def __repr__(self) -> str:
        return (
            f"CheckpointBatch({self.projection!r}, expected={self.expected}, target={self.target}, "
            f"claimed={self.claimed}, position={self.position})"
        )


@runtime_checkable
class CheckpointStore(Protocol):
    """Where projections keep the global position they have applied the event stream up to."""

    async def load(self, projection: str, *, initial: int = 0) -> int:
        """The position of *projection*'s checkpoint; a missing checkpoint is created at *initial*."""
        ...

    async def position(self, projection: str) -> int | None:
        """The position of *projection*'s checkpoint, ``None`` when it has none (it writes nothing: for
        monitoring a projection's lag behind ``last_position()``)."""
        ...

    def batch(
        self, projection: str, *, expected: int, position: int
    ) -> contextlib.AbstractAsyncContextManager[CheckpointBatch]:
        """A batch that moves *projection*'s checkpoint from *expected* to *position* (see :class:`CheckpointBatch`)."""
        ...

    async def reset(self, projection: str, position: int = 0) -> None:
        """Set *projection*'s checkpoint to *position* (0: its runner replays the whole stream)."""
        ...


@runtime_checkable
class ProjectionLease(Protocol):
    """A lease that makes one runner the active one for a projection (``LeaseLock`` is one)."""

    async def try_acquire(self, name: str, ttl: float) -> bool: ...

    async def extend(self, name: str, ttl: float) -> bool: ...

    async def release(self, name: str) -> None: ...


class InMemoryCheckpointStore:
    """Checkpoints kept in the process (tests and single-process development).

    Not transactional: a batch that fails keeps the position of the last event it applied, and the events
    before its failure are not applied again. Runners share the positions only when they share the instance.
    """

    def __init__(self) -> None:
        self._positions: dict[str, int] = {}

    async def load(self, projection: str, *, initial: int = 0) -> int:
        return self._positions.setdefault(projection, initial)

    async def position(self, projection: str) -> int | None:
        return self._positions.get(projection)

    @contextlib.asynccontextmanager
    async def batch(self, projection: str, *, expected: int, position: int) -> AsyncIterator[CheckpointBatch]:
        batch = CheckpointBatch(projection, expected=expected, target=position)
        current = self._positions.get(projection, 0)
        batch.claimed = current == expected
        if not batch.claimed:
            batch.position = current
            yield batch
            return
        try:
            yield batch
        except BaseException:
            if batch.progress is not None and self._positions.get(projection, 0) == expected:
                self._positions[projection] = batch.progress
                batch.position = batch.progress
            raise
        if self._positions.get(projection, 0) == expected:
            self._positions[projection] = position
            batch.position = position

    async def reset(self, projection: str, position: int = 0) -> None:
        self._positions[projection] = position


class SqlAlchemyCheckpointStore:
    """Checkpoints in the framework table ``pyfly_projection_checkpoints`` (see the module documentation).

    *datasource* is where they live, with the read models they track: an ``AsyncEngine``, a registry
    ``DataSource`` or a datasource name. *table_name* renames the table. With *create_table* false the store never
    creates its table and only checks it at :meth:`start` (migrations own the schema); the projection lease's
    table (``pyfly_locks``) is treated the same way.
    """

    def __init__(
        self, datasource: Any, *, table_name: str = "pyfly_projection_checkpoints", create_table: bool = True
    ) -> None:
        self._target = datasource
        self._table_name = table_name
        self._create_table = create_table
        self._table_object: Table | None = None
        self._lease: LeaseLock | None = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the checkpoint table and the lease table (``pyfly_locks``, for :meth:`projection_lease`) when
        allowed, then check them; raises ``FrameworkSchemaError`` when one is unusable.

        The application context starts the store; one built by hand starts on first use."""
        from pyfly.data.relational.framework_schema import ensure_tables, locks_table

        with outside_transaction():
            await ensure_tables(self._target, self._table, locks_table(), create=self._create_table)
        self._started = True

    async def _ready(self) -> None:
        if not self._started:
            await self.start()

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller)."""

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the store's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import projection_checkpoints_table

            self._table_object = projection_checkpoints_table(self._table_name)
        return self._table_object

    def projection_lease(self) -> LeaseLock:
        """The lease runners on this datasource take to be a projection's active runner (one per store)."""
        if self._lease is None:
            from pyfly.scheduling.adapters.lease_lock import LeaseLock

            self._lease = LeaseLock(self._target, create_table=self._create_table)
        return self._lease

    # ------------------------------------------------------------------
    # CheckpointStore
    # ------------------------------------------------------------------

    async def load(self, projection: str, *, initial: int = 0) -> int:
        from sqlalchemy import select

        from pyfly.data.relational.upsert import insert_if_absent

        await self._ready()
        table = self._table
        async with infrastructure_unit(self._target) as session:
            await insert_if_absent(
                session,
                table,
                {"projection": projection, "position": initial, "updated_at": datetime.now(UTC)},
                key=["projection"],
            )
            position = (
                await session.execute(select(table.c.position).where(table.c.projection == projection))
            ).scalar_one()
        return int(position)

    async def position(self, projection: str) -> int | None:
        from sqlalchemy import select

        await self._ready()
        table = self._table
        async with infrastructure_unit(self._target, read_only=True) as session:
            found = (await session.execute(select(table.c.position).where(table.c.projection == projection))).scalar()
        return None if found is None else int(found)

    @contextlib.asynccontextmanager
    async def batch(self, projection: str, *, expected: int, position: int) -> AsyncIterator[CheckpointBatch]:
        """The batch's unit of work: a new one on the store's datasource (it never joins a caller's), which
        commits the checkpoint and what the batch's events wrote together."""
        from sqlalchemy import update

        await self._ready()
        table = self._table
        batch = CheckpointBatch(projection, expected=expected, target=position)
        template = TransactionTemplate(resolve_manager(self._target), propagation=Propagation.REQUIRES_NEW)
        try:
            async with template.transaction() as unit:
                assert unit is not None
                moved = await unit.resource.execute(
                    update(table)
                    .where(table.c.projection == projection, table.c.position == expected)
                    .values(position=position, updated_at=datetime.now(UTC))
                )
                batch.claimed = int(moved.rowcount) == 1
                yield batch
        except BaseException:
            batch.position = expected
            raise
        batch.position = position if batch.claimed else expected

    async def reset(self, projection: str, position: int = 0) -> None:
        from pyfly.data.relational.upsert import upsert

        await self._ready()
        async with infrastructure_unit(self._target) as session:
            await upsert(
                session,
                self._table,
                {"projection": projection, "position": position, "updated_at": datetime.now(UTC)},
                key=["projection"],
            )
