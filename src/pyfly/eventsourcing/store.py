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
"""EventStore SPI plus in-memory and SQL adapters.

Every event an :class:`EventStore` holds has two places: its aggregate's ``sequence`` (1, 2, 3... per
aggregate, the optimistic-locking version) and a **global position** on the store's global stream, which
:meth:`EventStore.stream_all` pages by (``after_position``). Positions only move forward for a reader: once it has
seen position *p*, no event appears below *p* later, and an aggregate's events are on the stream in sequence
order. A projection can therefore keep one number as its checkpoint and never skip an event that committed late,
never get an event twice from paging, and never stall on events that share a timestamp. ``occurred_at`` is the
event's data, not a cursor.

:class:`SqlAlchemyEventStore` keeps the events in the framework table ``pyfly_event_store`` and gives out the
positions with one of two strategies, recorded per table in ``pyfly_event_store_head`` by the first store that
starts on it:

- ``head-row`` (every backend; the default): an event is inserted without a position, so it is not on the global
  stream yet. Reading the stream first gives the events that have committed since the last read their positions,
  in a short ``READ COMMITTED`` unit of its own that locks the table's head row (the last position given out): a
  position only ever goes to a committed event, and always above every position given before. An append never
  touches the head row, so business transactions do not wait for one another there, and none of them fails on
  it under snapshot isolation (MariaDB's ``REPEATABLE READ``, PostgreSQL's). The events of one round are ordered
  by the database's clock when it recorded them (``recorded_at``), then by aggregate and sequence: an aggregate's
  events keep their order, and an event appended after another one committed comes after it.
- ``xid8`` (PostgreSQL 13 or later; opt-in, an accelerator whose reads write nothing): an event's position is
  its writer's transaction id times 2**20 plus its place among that transaction's events, set as it is inserted,
  and a reader only sees the positions below its snapshot's horizon (``pg_snapshot_xmin(pg_current_snapshot())``):
  every transaction below it has ended, so no event can ever appear below what a reader has seen. The stream
  follows the order the writers took their transaction ids in, not the order they committed in. An aggregate's
  order is kept by a guard: an append whose transaction id is below the id of an event the aggregate already has
  is refused with a :class:`ConcurrencyError`, and the command runs again in a new unit (with a new id). The order
  across aggregates is not kept: an event appended after reading another aggregate's committed event can stream
  before that event. A transaction left open anywhere on the server holds the stream back until it ends
  (delivery waits; nothing is skipped).

Appends and reads run through :func:`~pyfly.data.transaction.infrastructure_unit`: inside a unit of work on the
store's datasource an aggregate's events are written in that unit and commit or roll back with the rest of the
business transaction (reads there see the unit's own events, which are not on the global stream until the unit
commits); outside one each call gets a short unit of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.data.transaction import (
    Isolation,
    Propagation,
    TransactionManager,
    TransactionTemplate,
    UnitOfWork,
    current_unit_of_work,
    infrastructure_unit,
    is_transaction_active,
    outside_transaction,
    resolve_manager,
)
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.upcaster import EventUpcaster
from pyfly.kernel.exceptions import OptimisticLockingFailureException

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

_logger = logging.getLogger(__name__)

POSITION_AUTO = "auto"
"""Follow the position strategy the event table recorded, when a store started on it before; ``head-row`` for a
new table, on every backend."""

POSITION_HEAD_ROW = "head-row"
"""Positions from the head row, given to the committed events when the stream is read (every backend; the
default)."""

POSITION_XID8 = "xid8"
"""Positions from the writer's PostgreSQL transaction id, read below the snapshot horizon (PostgreSQL 13+;
opt-in): reads write nothing, and the stream keeps each aggregate's order but not the order across aggregates."""

POSITION_STRATEGIES = (POSITION_AUTO, POSITION_HEAD_ROW, POSITION_XID8)

XID8_ORDINAL_BITS = 20
"""An ``xid8`` position is ``xid * 2**XID8_ORDINAL_BITS + ordinal``: a unit of work appends at most 2**20 events
to one event table."""

_XID8_SCALE = 1 << XID8_ORDINAL_BITS
_NUMBERING_BATCH = 1000  # committed events given positions per numbering unit
_NUMBERING_WINDOW = 100_000  # event ids one ordered read of the events without a position fetches (about 10 MB)
_ASSIGN_CHUNK = 500  # events per positions UPDATE (one CASE branch and one IN value each)


class ConcurrencyError(OptimisticLockingFailureException):
    """Optimistic-locking failure: the aggregate's stored version is not the one the append expected.

    Another writer appended to the aggregate since it was loaded: reload it and retry the command. It is the
    kernel's :class:`~pyfly.kernel.exceptions.OptimisticLockingFailureException` (HTTP 409, and a transient
    failure a message listener retries).
    """


def _apply_upcasters(envelope: StoredEventEnvelope, upcasters: Sequence[EventUpcaster]) -> StoredEventEnvelope:
    """Apply each registered upcaster (in order) that handles this envelope.

    Read paths (``load`` / ``stream_all``) run stored events through the
    configured upcasters so consumers always see current-schema events. The store's own placement of the
    event (its global position) survives an upcaster that builds a new envelope.
    """
    position = envelope.global_position
    for upcaster in upcasters:
        if upcaster.applies_to(envelope):
            envelope = upcaster.upcast(envelope)
    if envelope.global_position is None:
        envelope.global_position = position
    return envelope


@runtime_checkable
class EventStore(Protocol):
    """Append, load and stream events for aggregates.

    ``stream_all`` pages the global stream: the events after global position *after_position* (``None`` or 0:
    from the start), in position order, at most *limit* of them, each with its ``global_position`` set.
    *after_event_id* is the cursor of earlier releases (the event with that id is the last one seen); a store
    raises ``ValueError`` for an id it does not have on its stream.
    """

    async def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        events: list[StoredEventEnvelope],
        *,
        expected_version: int,
    ) -> None: ...

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]: ...

    async def stream_all(
        self,
        *,
        after_position: int | None = None,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[StoredEventEnvelope]: ...

    async def latest_version(self, aggregate_id: str) -> int: ...


def _one_cursor(after_position: int | None, after_event_id: str | None) -> None:
    if after_position is not None and after_event_id is not None:
        raise ValueError("stream_all takes after_position or after_event_id, not both")


class InMemoryEventStore:
    """Default zero-dep adapter: list per aggregate, global event log.

    The global position of an event is its place in the log (1, 2, 3...): appends run one at a time under a
    lock, so the log's order is commit order. It keeps nothing across restarts and does not take part in units
    of work.
    """

    def __init__(self, upcasters: Sequence[EventUpcaster] = ()) -> None:
        self._by_aggregate: dict[str, list[StoredEventEnvelope]] = {}
        self._all: list[StoredEventEnvelope] = []
        self._lock = asyncio.Lock()
        self._upcasters: tuple[EventUpcaster, ...] = tuple(upcasters)

    async def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        events: list[StoredEventEnvelope],
        *,
        expected_version: int,
    ) -> None:
        async with self._lock:
            current = self._by_aggregate.get(aggregate_id, [])
            if len(current) != expected_version:
                msg = f"expected version {expected_version}, found {len(current)}"
                raise ConcurrencyError(msg, context={"aggregate_id": aggregate_id})
            for evt in events:
                evt.aggregate_id = aggregate_id
                evt.aggregate_type = aggregate_type
                evt.sequence = len(current) + 1
                evt.global_position = len(self._all) + 1
                current.append(evt)
                self._all.append(evt)
            self._by_aggregate[aggregate_id] = current

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]:
        async with self._lock:
            events = self._by_aggregate.get(aggregate_id, [])
            return [_apply_upcasters(e, self._upcasters) for e in events if e.sequence > after_sequence]

    async def stream_all(
        self,
        *,
        after_position: int | None = None,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[StoredEventEnvelope]:
        _one_cursor(after_position, after_event_id)
        async with self._lock:
            start = max(after_position or 0, 0)
            if after_event_id is not None:
                found = next((i for i, evt in enumerate(self._all) if evt.event_id == after_event_id), None)
                if found is None:
                    raise ValueError(f"Unknown event id {after_event_id!r}: it is not on this store's global stream")
                start = found + 1
            raw = list(self._all[start : start + limit])
        return [_apply_upcasters(e, self._upcasters) for e in raw]

    async def latest_version(self, aggregate_id: str) -> int:
        async with self._lock:
            return len(self._by_aggregate.get(aggregate_id, []))

    async def last_position(self) -> int:
        """The global position of the last event on the stream (0 when it is empty)."""
        async with self._lock:
            return len(self._all)


# Per unit of work, per event table: the next ordinal of the unit's transaction (``xid8``). Every store instance on
# one table shares it, so a unit's positions never collide.
_UNIT_ORDINALS: weakref.WeakKeyDictionary[UnitOfWork, dict[str, int]] = weakref.WeakKeyDictionary()


class _Backlog:
    """The committed events without a position, in the order they get theirs, from one ordered read of them
    (nothing can serve that order but a sort, so a store reads it once per window, not once per numbering round).

    ``reached`` is how far the store's committed rounds have got through the list. ``head`` is the head row's
    position after the last of them: every numbering moves the head row, so while it is still there no other store
    has numbered anything since, and the rest of the list has no position yet. ``None`` when that is not known: the
    rest of the list is checked again as it is numbered."""

    __slots__ = ("event_ids", "head", "reached")

    def __init__(self, event_ids: list[str]) -> None:
        self.event_ids = event_ids
        self.reached = 0
        self.head: int | None = None


class SqlAlchemyEventStore:
    """Async SQL adapter for the event store (see the module documentation).

    *engine* is where the events live: an ``AsyncEngine``, a registry ``DataSource`` or a datasource name.
    *table_name* and *head_table_name* rename the framework tables (declared on the framework metadata under
    those names). With *create_table* false the store never creates its tables and only checks them at
    :meth:`start`. *position_strategy* is ``auto`` (the default), ``head-row`` or ``xid8``; the strategy an event
    table's positions follow is recorded by the first store that starts on it (``head-row`` when it is left to
    ``auto``), ``auto`` follows it, and an explicit strategy that differs from it is refused at start (two
    strategies on one table would skip events).

    The application context starts the store; one built by hand starts on first use, or with :meth:`start`.
    Events a table got before it had global positions (rows an earlier release wrote, after a migration added
    the column) are placed on the stream like any other event without one, oldest ``occurred_at`` first: by the
    readers with ``head-row``, at start with ``xid8``.
    """

    def __init__(
        self,
        engine: Any,
        upcasters: Sequence[EventUpcaster] = (),
        *,
        table_name: str = "pyfly_event_store",
        head_table_name: str = "pyfly_event_store_head",
        create_table: bool = True,
        position_strategy: str = POSITION_AUTO,
    ) -> None:
        if position_strategy not in POSITION_STRATEGIES:
            raise ValueError(
                f"Unknown event store position strategy {position_strategy!r}; valid: {', '.join(POSITION_STRATEGIES)}"
            )
        self._target = engine
        self._upcasters: tuple[EventUpcaster, ...] = tuple(upcasters)
        self._table_name = table_name
        self._head_table_name = head_table_name
        self._create_table = create_table
        self._configured = position_strategy
        self._strategy: str | None = None
        self._backend = ""
        self._events_table: Table | None = None
        self._head_table: Table | None = None
        self._backlog = _Backlog([])  # the events without a position, as the numbering rounds work through them

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the tables when allowed and check them, and settle the position strategy; with ``xid8``, whose
        readers never number, also give the committed events without a position (an earlier release's) theirs.
        Raises ``FrameworkSchemaError`` when a table is unusable, or the configured strategy is not the one the
        table recorded.

        With ``head-row`` the start numbers nothing: a backlog of events without a position (a large table of an
        earlier release) is numbered by the readers as they page, and never holds up the start."""
        await self._start(create=self._create_table)

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller)."""

    async def initialize(self) -> None:
        """Create the tables if they do not exist and start (kept for callers that set the store up by hand)."""
        await self._start(create=True)

    async def _start(self, *, create: bool) -> None:
        from pyfly.data.relational.framework_schema import ensure_tables
        from pyfly.data.relational.upsert import backend_name

        # Setting up is never part of a caller's transaction, even when the first append starts the store.
        with outside_transaction():
            await ensure_tables(self._target, self._events, self._head, create=create)
            self._backend = backend_name(self.engine)
            strategy = await self._settle_strategy()
            if strategy == POSITION_XID8:
                await self._number_unnumbered(resolve_manager(self._target))
        self._strategy = strategy

    async def _ready(self) -> str:
        strategy = self._strategy
        if strategy is None:
            await self.start()
            strategy = self._strategy
            assert strategy is not None
        return strategy

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the store's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    @property
    def position_strategy(self) -> str:
        """The strategy the store's positions follow (``head-row`` or ``xid8`` once started; until then, the
        configured one)."""
        return self._strategy or self._configured

    @property
    def _events(self) -> Table:
        if self._events_table is None:
            from pyfly.data.relational.framework_schema import event_store_table

            self._events_table = event_store_table(self._table_name)
        return self._events_table

    @property
    def _head(self) -> Table:
        if self._head_table is None:
            from pyfly.data.relational.framework_schema import event_store_head_table

            self._head_table = event_store_head_table(self._head_table_name)
        return self._head_table

    # ------------------------------------------------------------------
    # EventStore
    # ------------------------------------------------------------------

    async def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        events: list[StoredEventEnvelope],
        *,
        expected_version: int,
    ) -> None:
        """Append *events* to the aggregate, whose stored version must be *expected_version*; raises
        :class:`ConcurrencyError` otherwise, and when a concurrent writer appended the same sequence first. With
        the ``xid8`` strategy it also raises it when the aggregate has an event of a transaction whose id is
        above this unit's: run the command again in a new unit, whose id is above it.

        The events are sent in one ``INSERT`` (a multi-row statement or a driver batch)."""
        from sqlalchemy import func, insert, select
        from sqlalchemy.exc import DBAPIError

        strategy = await self._ready()
        table = self._events
        manager = resolve_manager(self._target)
        guarded = strategy == POSITION_XID8 and bool(events)
        checked: list[ColumnElement[Any]] = [func.coalesce(func.max(table.c.sequence), 0)]
        if guarded:
            checked += [func.max(table.c.global_position), _xid8_base()]
        async with infrastructure_unit(manager) as session:
            # Read the current version INSIDE the write unit, so the check and the insert are one transaction;
            # the UNIQUE (aggregate_id, sequence) constraint is the backstop against a concurrent writer.
            found = (await session.execute(select(*checked).where(table.c.aggregate_id == aggregate_id))).one()
            latest = int(found[0])
            if latest != expected_version:
                raise ConcurrencyError(
                    f"expected version {expected_version}, found {latest}",
                    context={"aggregate_id": aggregate_id, "expected_version": expected_version},
                )
            if not events:
                return
            if guarded and found[1] is not None and int(found[1]) >= int(found[2]) + _XID8_SCALE:
                # The aggregate's last event belongs to a transaction whose id is above this unit's: an event
                # appended now would be placed before it on the global stream.
                raise ConcurrencyError(
                    f"aggregate {aggregate_id!r} has an event of a transaction with a later id than this unit's "
                    "(xid8 positions): run the command again in a new unit of work",
                    context={"aggregate_id": aggregate_id, "expected_version": expected_version},
                )
            rows = []
            for index, evt in enumerate(events, start=1):
                evt.aggregate_id = aggregate_id
                evt.aggregate_type = aggregate_type
                evt.sequence = expected_version + index
                evt.global_position = None
                rows.append(
                    {
                        "event_id": evt.event_id,
                        "aggregate_id": aggregate_id,
                        "aggregate_type": aggregate_type,
                        "sequence": evt.sequence,
                        "event_type": evt.event_type,
                        "payload": evt.to_json(),
                        "metadata": json.dumps(evt.metadata, default=str),
                        "occurred_at": evt.occurred_at,
                        "version": evt.version,
                        "tenant_id": evt.tenant_id,
                    }
                )
            statement = insert(table).values(recorded_at=_database_now(self._backend))
            if strategy == POSITION_XID8:
                unit = current_unit_of_work(manager.datasource)
                assert unit is not None  # infrastructure_unit bound one
                first = self._reserve_ordinals(unit, len(rows))
                for offset, row in enumerate(rows):
                    row["pyfly_ordinal"] = first + offset
                statement = statement.values(global_position=_xid8_position())
            try:
                await session.execute(statement, rows)
            except DBAPIError as error:
                if not _concurrent_append(error):
                    raise
                # A concurrent writer committed the same (aggregate_id, sequence) between our check and insert.
                raise ConcurrencyError(
                    f"concurrent append for aggregate {aggregate_id!r} at version {expected_version}",
                    context={"aggregate_id": aggregate_id, "expected_version": expected_version},
                ) from error

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]:
        from sqlalchemy import select

        await self._ready()
        table = self._events
        statement = (
            select(table.c.payload, table.c.global_position)
            .where(table.c.aggregate_id == aggregate_id, table.c.sequence > after_sequence)
            .order_by(table.c.sequence)
        )
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (await session.execute(statement)).all()
        return [self._envelope(payload, position) for payload, position in rows]

    async def stream_all(
        self,
        *,
        after_position: int | None = None,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[StoredEventEnvelope]:
        """The committed events after global position *after_position*, in position order (see
        :class:`EventStore`); inside a unit of work, that unit's own events are not on the stream yet.

        With ``head-row`` a read outside a unit of work on the store's datasource first gives the committed events
        without a position theirs (as many as the page needs). A read inside such a unit numbers nothing: it shows
        the events a reader outside one has numbered, so a reader that only ever runs inside one (a
        ``@transactional(read_only=True)`` endpoint) sees new events once another reader, a projection runner or
        :meth:`last_position`, has numbered them."""
        from sqlalchemy import select

        _one_cursor(after_position, after_event_id)
        strategy = await self._ready()
        table = self._events
        manager = resolve_manager(self._target)
        probe = self._numbers_on_read(strategy, manager)
        while True:
            async with infrastructure_unit(manager, read_only=True) as session:
                if not (probe and await self._unnumbered(session)):
                    after = max(after_position or 0, 0)
                    if after_event_id is not None:
                        found = (
                            await session.execute(
                                select(table.c.global_position).where(table.c.event_id == after_event_id)
                            )
                        ).first()
                        if found is None or found[0] is None:
                            raise ValueError(
                                f"Unknown event id {after_event_id!r}: it is not on the global stream of {table.name}"
                            )
                        after = int(found[0])
                    page = select(table.c.payload, table.c.global_position).where(table.c.global_position > after)
                    if strategy == POSITION_XID8:
                        page = page.where(table.c.global_position < _xid8_horizon())
                    rows = (await session.execute(page.order_by(table.c.global_position).limit(limit))).all()
                    return [self._envelope(payload, position) for payload, position in rows]
            await self._number_committed(manager, at_least=limit)
            probe = False

    async def latest_version(self, aggregate_id: str) -> int:
        from sqlalchemy import func, select

        await self._ready()
        table = self._events
        statement = select(func.coalesce(func.max(table.c.sequence), 0)).where(table.c.aggregate_id == aggregate_id)
        async with infrastructure_unit(self._target, read_only=True) as session:
            return int((await session.execute(statement)).scalar_one())

    async def last_position(self) -> int:
        """The global position of the last event a reader can see on the stream now (0 when there is none):
        where a new projection that should skip the history starts.

        With ``head-row``, outside a unit of work on the store's datasource, it first gives every committed event
        without a position its own: over the backlog of a large table an earlier release filled, that takes as
        long as numbering all of it (the event-sourcing guide gives the figures, and SQL that numbers such a
        table before the upgraded application starts)."""
        from sqlalchemy import func, select

        strategy = await self._ready()
        table = self._events
        manager = resolve_manager(self._target)
        last = select(func.coalesce(func.max(table.c.global_position), 0))
        if strategy == POSITION_XID8:
            last = last.where(table.c.global_position < _xid8_horizon())
        probe = self._numbers_on_read(strategy, manager)
        while True:
            async with infrastructure_unit(manager, read_only=True) as session:
                if not (probe and await self._unnumbered(session)):
                    return int((await session.execute(last)).scalar_one())
            await self._number_committed(manager)
            probe = False

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def _envelope(self, payload: str, position: int | None) -> StoredEventEnvelope:
        envelope = StoredEventEnvelope.from_json(payload)
        envelope.global_position = None if position is None else int(position)
        return _apply_upcasters(envelope, self._upcasters)

    def _reserve_ordinals(self, unit: UnitOfWork, count: int) -> int:
        """The first of *count* ordinals of the unit's transaction on this event table (``xid8``)."""
        ordinals = _UNIT_ORDINALS.setdefault(unit, {})
        first = ordinals.get(self._table_name, 0)
        if first + count > _XID8_SCALE:
            raise ValueError(
                f"A unit of work appends at most {_XID8_SCALE} events to one event table ({self._table_name})"
            )
        ordinals[self._table_name] = first + count
        return first

    @staticmethod
    def _numbers_on_read(strategy: str, manager: TransactionManager) -> bool:
        """Whether a read gives the committed events without a position theirs first (``head-row``).

        Not from inside a unit of work on the datasource: that unit's own events are not committed, and on
        SQLite its write lock would keep the numbering unit waiting. The read then shows what has a position."""
        return strategy == POSITION_HEAD_ROW and not is_transaction_active(manager.datasource)

    async def _unnumbered(self, session: AsyncSession) -> bool:
        """Whether a committed event has no global position yet (one indexed probe)."""
        from sqlalchemy import select

        table = self._events
        probe = select(table.c.event_id).where(table.c.global_position.is_(None)).limit(1)
        return (await session.execute(probe)).first() is not None

    async def _number_unnumbered(self, manager: TransactionManager) -> None:
        """At start with ``xid8``, whose readers never number: give the committed events without a position (an
        earlier release's rows) theirs, from the head row.

        When the table has ``xid8`` positions already (above the head row's), those events (a writer of an earlier
        release still running after the upgrade) get positions below them, which a projection may have passed: a
        WARNING says so."""
        from sqlalchemy import func, select

        head, table = self._head, self._events
        async with infrastructure_unit(manager, read_only=True) as session:
            if not await self._unnumbered(session):
                return
            highest = (
                await session.execute(
                    select(
                        func.max(table.c.global_position),
                        select(head.c.position).where(head.c.store == self._table_name).scalar_subquery(),
                    )
                )
            ).one()
        if highest[0] is not None and int(highest[0]) > int(highest[1] or 0):
            _logger.warning(
                "event_store_positions_below_readers",
                extra={
                    "table": self._table_name,
                    "hint": "events without a global position get theirs from the head row, below the xid8 "
                    "positions projections may have passed already, which then skip them: stop the writers of "
                    "an earlier release before starting this one",
                },
            )
        await self._number_committed(manager)

    async def _number_committed(self, manager: TransactionManager, *, at_least: int | None = None) -> None:
        """Give the committed events that have no global position theirs, in units of their own, one
        :data:`_NUMBERING_BATCH` at a time (``READ COMMITTED`` where the backend has it: each round sees every
        event committed before it, and no snapshot conflict can fail it), until none is left or, with *at_least*,
        that many have been numbered: a page read numbers what its page needs (its *limit*), so a backlog is
        numbered while the pages are read rather than before the first one, and a page is only short when the
        stream has no more committed events."""
        isolation = Isolation.READ_COMMITTED
        if not manager.capabilities.supports_isolation(isolation):
            isolation = Isolation.DEFAULT  # SQLite: one writer at a time, which is stronger
        template = TransactionTemplate(manager, propagation=Propagation.REQUIRES_NEW, isolation=isolation)
        numbered = 0
        with outside_transaction():
            while True:
                async with template.transaction() as unit:
                    assert unit is not None
                    count, more, backlog, reached, head = await self._number_round(unit.resource)
                # Only once the round has committed are the events it went through known to have positions.
                if backlog is self._backlog and reached > backlog.reached:
                    backlog.reached, backlog.head = reached, head
                    if reached >= len(backlog.event_ids):
                        self._backlog = _Backlog([])
                numbered += count
                if not more or (at_least is not None and numbered >= at_least):
                    break
        _logger.debug("event_store_events_numbered", extra={"table": self._table_name, "events": numbered})

    async def _number_round(self, session: AsyncSession) -> tuple[int, bool, _Backlog, int, int | None]:
        """One numbering round: lock the head row, give the next positions to the next committed events of the
        store's backlog list that still have none (:meth:`_read_backlog`, read again when the list is used up),
        move the head row on. Returns how many it numbered, whether events may be left without a position, the
        list it took them from and how far it got through it, and the head row's new position when no other
        store had numbered anything since the list was read (``None`` otherwise: see :class:`_Backlog`)."""
        from sqlalchemy import case, select, update

        from pyfly.data.relational.framework_schema import FrameworkSchemaError

        head, table = self._head, self._events
        last = (
            await session.execute(select(head.c.position).where(head.c.store == self._table_name).with_for_update())
        ).scalar()
        if last is None:
            raise FrameworkSchemaError(
                f"The head row of event table {self._table_name} is missing from {head.name}: starting the event "
                "store creates it again (it takes the positions on from the highest one in the table)."
            )
        base = int(last)
        backlog = self._backlog
        reached, fresh = backlog.reached, False
        alone = backlog.head == base
        while True:
            if reached >= len(backlog.event_ids):
                backlog = self._backlog = _Backlog(await self._read_backlog(session))
                reached, fresh, alone = 0, True, True  # read under the head row's lock: current
                if not backlog.event_ids:
                    return 0, False, backlog, 0, None
            chunk = backlog.event_ids[reached : reached + _NUMBERING_BATCH]
            # Unless nobody else has numbered anything since the list was read, its events are checked again, and
            # those numbered meanwhile are passed over.
            event_ids = chunk if alone else await self._still_unnumbered(session, chunk)
            reached += len(chunk)
            if event_ids:
                break
        await session.execute(
            update(head).where(head.c.store == self._table_name).values(position=base + len(event_ids))
        )
        for start in range(0, len(event_ids), _ASSIGN_CHUNK):
            part = event_ids[start : start + _ASSIGN_CHUNK]
            positions = case(
                {event_id: base + start + index + 1 for index, event_id in enumerate(part)}, value=table.c.event_id
            )
            await session.execute(update(table).where(table.c.event_id.in_(part)).values(global_position=positions))
        if reached < len(backlog.event_ids):
            more = True
        elif fresh:
            more = len(backlog.event_ids) == _NUMBERING_WINDOW  # the read stopped at a full window: more may follow
        else:
            more = await self._unnumbered(session)  # an older list is used up: events may have committed since
        return len(event_ids), more, backlog, reached, base + len(event_ids) if alone else None

    async def _read_backlog(self, session: AsyncSession) -> list[str]:
        """The committed events without a position, up to :data:`_NUMBERING_WINDOW` of them, in the order they get
        theirs: oldest record first (``recorded_at``, the database's clock; an earlier release's rows, which have
        none, by ``occurred_at``), an aggregate's in sequence order.

        No index serves that order, so the read sorts every event without a position: it happens once per window,
        and the rounds number the list it returns in turn (a backlog of N events costs N/window sorts, not
        N/round). Events that commit after the read are numbered after the list, above every position it got."""
        from sqlalchemy import func, select

        table = self._events
        pending = (
            select(table.c.event_id)
            .where(table.c.global_position.is_(None))
            .order_by(func.coalesce(table.c.recorded_at, table.c.occurred_at), table.c.aggregate_id, table.c.sequence)
            .limit(_NUMBERING_WINDOW)
        )
        found: Sequence[Any] = (await session.execute(pending)).scalars().all()
        return [str(event_id) for event_id in found]

    async def _still_unnumbered(self, session: AsyncSession, event_ids: list[str]) -> list[str]:
        """Those of *event_ids* that have no position yet, in their order: one read by primary key per
        :data:`_ASSIGN_CHUNK` of them (with ``global_position IS NULL`` in the query, a planner may walk that
        index over the whole backlog instead)."""
        from sqlalchemy import select

        table = self._events
        left: set[str] = set()
        for start in range(0, len(event_ids), _ASSIGN_CHUNK):
            part = event_ids[start : start + _ASSIGN_CHUNK]
            found = select(table.c.event_id, table.c.global_position).where(table.c.event_id.in_(part))
            rows: Sequence[Any] = (await session.execute(found)).all()
            left.update(str(event_id) for event_id, position in rows if position is None)
        return [event_id for event_id in event_ids if event_id in left]

    async def _settle_strategy(self) -> str:
        """The strategy of the event table: the one recorded in its head row, recorded now by this store when
        the table has none. Creates the head row when it is missing (and writes nothing when it is there: a store
        first used inside a unit of work that holds SQLite's write lock does not wait for that unit)."""
        from sqlalchemy import func, select

        from pyfly.data.relational.framework_schema import FrameworkSchemaError
        from pyfly.data.relational.upsert import insert_if_absent

        backend = self._backend
        if self._configured == POSITION_XID8 and not await self._xid8_ready():
            raise ValueError(
                f"The xid8 position strategy needs PostgreSQL 13 or later; the event store's datasource is {backend}. "
                f"Use position_strategy={POSITION_HEAD_ROW!r} (or {POSITION_AUTO!r})."
            )
        wanted = POSITION_HEAD_ROW if self._configured == POSITION_AUTO else self._configured
        head, table = self._head, self._events
        strategy = select(head.c.strategy).where(head.c.store == self._table_name)
        async with infrastructure_unit(self._target, read_only=True) as session:
            recorded = (await session.execute(strategy)).scalar()
        if recorded is None:
            async with infrastructure_unit(self._target) as session:
                # A head row created after its table lost it takes the positions on from the highest one given out.
                highest = select(func.coalesce(func.max(table.c.global_position), 0)).scalar_subquery()
                await insert_if_absent(
                    session,
                    head,
                    {"store": self._table_name, "position": highest, "strategy": wanted},
                    key=["store"],
                )
                recorded = (await session.execute(strategy)).scalar_one()
        recorded = str(recorded)
        if recorded == wanted:
            return wanted
        if recorded not in (POSITION_HEAD_ROW, POSITION_XID8):
            raise FrameworkSchemaError(
                f"{head.name} records position strategy {recorded!r} for {self._table_name}, which this release does "
                f"not know ({POSITION_HEAD_ROW!r} or {POSITION_XID8!r})"
            )
        if self._configured != POSITION_AUTO:
            raise FrameworkSchemaError(
                f"The events of {self._table_name} take their global positions with the {recorded!r} strategy "
                f"(recorded in {head.name}), but this store is configured for {self._configured!r}: two strategies on "
                f"one table would let readers skip events. Leave position_strategy at {POSITION_AUTO!r} "
                "(pyfly.eventsourcing.store.position-strategy), or migrate the table with every writer stopped."
            )
        if recorded == POSITION_XID8 and not await self._xid8_ready():
            raise FrameworkSchemaError(
                f"The events of {self._table_name} take their global positions with the xid8 strategy (recorded in "
                f"{head.name}), which needs PostgreSQL 13 or later; this datasource is {backend} without it."
            )
        _logger.info("event_store_position_strategy_recorded", extra={"table": self._table_name, "strategy": recorded})
        return recorded

    async def _xid8_ready(self) -> bool:
        """Whether the store's datasource can give ``xid8`` positions (PostgreSQL 13 or later)."""
        return self._backend == "postgresql" and await self._xid8_available()

    async def _xid8_available(self) -> bool:
        """Whether the server has the ``xid8`` snapshot functions (PostgreSQL 13 or later; not every server that
        speaks PostgreSQL's protocol does)."""
        from sqlalchemy import func, select
        from sqlalchemy.exc import DBAPIError

        try:
            async with self.engine.connect() as connection:
                await connection.execute(select(func.pg_snapshot_xmin(func.pg_current_snapshot())))
        except DBAPIError:
            _logger.debug("event_store_xid8_unavailable", exc_info=True)
            return False
        return True


def _concurrent_append(error: BaseException) -> bool:
    """Whether an append's ``INSERT`` failed because another writer appended to the aggregate first: a unique
    violation, or MariaDB's snapshot-isolation conflict (translated to the kernel's exceptions)."""
    from pyfly.data.exception_translation import translate_exception
    from pyfly.kernel.exceptions import DuplicateKeyException

    return isinstance(translate_exception(error), (DuplicateKeyException, OptimisticLockingFailureException))


def _database_now(backend: str) -> ColumnElement[datetime] | datetime:
    """When the database records a row, by its own clock (one clock for every writer), in UTC with microseconds;
    the application's clock on a backend without such a function known here."""
    from sqlalchemy import DateTime, func, literal_column

    if backend == "postgresql":
        return func.clock_timestamp()  # the statement's own time (now() is the transaction's start)
    if backend in ("mysql", "mariadb"):
        return literal_column("UTC_TIMESTAMP(6)", DateTime())
    if backend == "sqlite":
        return func.strftime("%Y-%m-%d %H:%M:%f000", "now")
    if backend == "mssql":
        return func.sysutcdatetime()
    return datetime.now(UTC)


def _xid8_base() -> ColumnElement[int]:
    """The first position of the current transaction (``xid8``): its id (assigned now when it has none yet; the
    top-level transaction's inside a savepoint) times 2**20."""
    from sqlalchemy import BigInteger, Text, cast, func

    return cast(cast(func.pg_current_xact_id(), Text), BigInteger) * _XID8_SCALE


def _xid8_position() -> ColumnElement[int]:
    """The position of an event inserted now (``xid8``): the transaction's first position plus the row's ordinal
    (the ``pyfly_ordinal`` parameter)."""
    from sqlalchemy import BigInteger, bindparam

    return _xid8_base() + bindparam("pyfly_ordinal", type_=BigInteger)


def _xid8_horizon() -> ColumnElement[int]:
    """The lowest position a transaction still running could give (``xid8``): readers see the positions below."""
    from sqlalchemy import BigInteger, Text, cast, func

    xmin = cast(cast(func.pg_snapshot_xmin(func.pg_current_snapshot()), Text), BigInteger)
    return xmin * _XID8_SCALE
