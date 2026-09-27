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
"""The transactional outbox: events written in the publisher's unit of work, delivered by a relay.

An event is published by inserting a row into the outbox table *in the unit of work of the code that
publishes it* (:func:`~pyfly.data.transaction.infrastructure_unit`): when that unit rolls back, the event
was never published, and when it commits, the event is there for good. A relay in the background then
hands every committed event to its subscribers, at least once. There is no dual write, and nothing is lost
to the order in which transactions commit.

Four framework tables (:mod:`pyfly.data.relational.framework_schema`) hold an outbox, on any SQL backend:

- ``pyfly_outbox_events``: one row per event;
- ``pyfly_outbox_consumers``: the consumer groups, and the destinations each consumes;
- ``pyfly_outbox_deliveries``: what the outbox still owes, one row per consumer group and event. Publishing
  an event inserts its row for every group registered for its destination (or for the groups named);
- ``pyfly_outbox_dead_letters``: the deliveries a subscription failed to handle on every attempt.

**Delivery is claimed by state, not read by a cursor.** A group's relay claims the delivery rows whose
``available_at`` has passed: the claim moves them a lease ahead (``claim_timeout``) and records who took
them, in one short unit of its own, with ``FOR UPDATE SKIP LOCKED`` where the backend has it (PostgreSQL,
MySQL 8, MariaDB 10.6) and an optimistic ``UPDATE ... WHERE available_at <= now`` elsewhere (SQLite has one
writer, so the claim is serialized there anyway). A row that commits late is claimed when it becomes
visible; the relays of one group on several nodes share the rows without taking one twice; a relay that
dies leaves its rows to be claimed again once the lease ends. An id cursor, and the snapshot guard
(PostgreSQL's ``xid8``) that such a cursor needs to be safe, have no part in it: a transaction that commits
behind a later one cannot be skipped.

**Each subscription of a group is settled on its own.** A delivery runs every subscription of the group whose
pattern matches the event type; the ones that succeed are recorded in the delivery row, so a later attempt
runs only the ones that failed, and the other deliveries of the group go on meanwhile. A failure is
attempted again after a back-off (:class:`~pyfly.messaging.listener_container.RetryPolicy`), and after the
last attempt the event is copied into the dead-letter table for that subscription
(:class:`~pyfly.eda.types.ErrorStrategy` chooses otherwise). A handler that hangs is cancelled after
``handler_timeout``, and that counts as a failure.

**Retention.** A relay deletes, in batches, the events every group has handled (and that are older than
:attr:`Retention.delivered`), and, when :attr:`Retention.max_age` is set, every event older than that with
the deliveries still owed for it.

**Wake-ups.** A relay polls every ``poll_interval`` seconds; a publish in the same process wakes it once the
publishing unit commits, and on PostgreSQL a ``NOTIFY`` sent in the publishing unit (delivered only if it
commits) wakes the relays of every process (:class:`~pyfly.eda.adapters.database.DatabaseEventBus`).

The relay is a :data:`~pyfly.kernel.lifecycle.CONSUMER_PHASE` lifecycle bean: the application context stops
it before any ``@pre_destroy``. It runs detached from the units of work of the code that starts it
(:func:`~pyfly.data.transaction.detached`), and a handler runs in the unit of work its ``@transactional``
declares (the listener container's rules: :class:`~pyfly.messaging.listener_container.ListenerInvoker`).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fnmatch
import functools
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.domain.domain_event import to_json_value
from pyfly.eda.dlq import EdaDeadLetterEntry, EdaDeadLetterStore
from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from pyfly.kernel.lifecycle import CONSUMER_PHASE

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from pyfly.data.transaction import TransactionManager
    from pyfly.messaging.listener_container import ListenerContainerSettings, ListenerInvoker, RetryPolicy

_logger = logging.getLogger(__name__)

EVERY_DESTINATION = "*"
"""The destination a consumer group registers to receive the events of every destination."""

DEFAULT_PREFIX = "pyfly_outbox"
"""The prefix of the default outbox tables (``pyfly_outbox_events`` and the others)."""

_MAX_ERROR_LENGTH = 4000


# ---------------------------------------------------------------------------------------------------------
# Settings and values
# ---------------------------------------------------------------------------------------------------------


class StartPosition(enum.Enum):
    """Where a consumer group that registers for the first time starts."""

    LATEST = "latest"
    """With the events published after it registered (what a new Kafka, Redis or RabbitMQ consumer gets)."""
    EARLIEST = "earliest"
    """With every event the outbox still holds for its destinations, then the ones published after."""

    @classmethod
    def of(cls, value: StartPosition | str) -> StartPosition:
        """*value* as a start position (``latest`` or ``earliest``, any case)."""
        if isinstance(value, StartPosition):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            raise ValueError(f"A start position is 'latest' or 'earliest', got {value!r}") from None


@dataclass(frozen=True)
class Retention:
    """How long the outbox keeps events.

    - *delivered*: an event every group has handled is deleted once it is older than this (``None``: never);
    - *max_age*: an event older than this is deleted with the deliveries still owed for it, whether or not
      they were made (``None``: never; a group that stops consuming then keeps its events);
    - *interval*: how often a relay sweeps, and *batch_size* how many events one statement deletes.
    """

    delivered: timedelta | None = timedelta(hours=1)
    max_age: timedelta | None = None
    interval: timedelta = timedelta(minutes=1)
    batch_size: int = 1000

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"Retention.batch_size must be at least 1, got {self.batch_size}")
        for name in ("delivered", "max_age"):
            value = getattr(self, name)
            if value is not None and value < timedelta(0):
                raise ValueError(f"Retention.{name} must not be negative, got {value}")


@dataclass(frozen=True)
class PruneResult:
    """What one retention sweep deleted: events every group had handled, and events past their maximum age
    (with *undelivered* deliveries still owed for them)."""

    delivered: int = 0
    expired: int = 0
    undelivered: int = 0


@dataclass(frozen=True)
class OutboxTables:
    """The four tables of one outbox (see the module documentation)."""

    events: Table
    deliveries: Table
    consumers: Table
    dead_letters: Table

    @classmethod
    def named(cls, prefix: str = DEFAULT_PREFIX) -> OutboxTables:
        """The tables ``<prefix>_events``, ``<prefix>_deliveries``, ``<prefix>_consumers`` and
        ``<prefix>_dead_letters``, declared on the framework metadata."""
        from pyfly.data.relational.framework_schema import (
            outbox_consumers_table,
            outbox_dead_letters_table,
            outbox_deliveries_table,
            outbox_events_table,
        )

        return cls(
            events=outbox_events_table(f"{prefix}_events"),
            deliveries=outbox_deliveries_table(f"{prefix}_deliveries"),
            consumers=outbox_consumers_table(f"{prefix}_consumers"),
            dead_letters=outbox_dead_letters_table(f"{prefix}_dead_letters"),
        )

    def all(self) -> tuple[Table, ...]:
        """Every table, in creation order."""
        return (self.events, self.deliveries, self.consumers, self.dead_letters)


@dataclass(frozen=True)
class Delivery:
    """A delivery a relay claimed: the event, which attempt this is, and the subscriptions that handled it
    already. *token* identifies the claim; settling the delivery needs it."""

    outbox_id: int
    group: str
    envelope: EventEnvelope
    attempts: int
    done: frozenset[str]
    token: str
    last_error: str | None = None


@dataclass(frozen=True)
class PendingDelivery:
    """A delivery the outbox still owes a group, as :meth:`Outbox.pending` reads it."""

    outbox_id: int
    envelope: EventEnvelope
    attempts: int
    available_at: datetime
    last_error: str | None


def encode_json(value: Any) -> str:
    """*value* as compact JSON; instants, decimals, UUIDs, enums and dataclasses by
    :func:`~pyfly.domain.domain_event.to_json_value`."""
    return json.dumps(value, default=to_json_value, separators=(",", ":"), ensure_ascii=False)


def _truncated(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_LENGTH else text[: _MAX_ERROR_LENGTH - 1] + "…"


def describe_error(error: BaseException) -> str:
    """``Type: message`` of *error*, bounded, for ``last_error``."""
    return _truncated(f"{type(error).__name__}: {error}")


# ---------------------------------------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------------------------------------


class _LeaseLost(Exception):
    """The delivery was claimed again by another relay after this one's lease ended: roll back its settling."""


class Outbox:
    """The outbox tables on one datasource, and the statements that write and read them.

    *datasource* is where the tables live: a datasource name, a registry ``DataSource``, an ``AsyncEngine``,
    a transaction manager, or ``None`` (the default datasource). *tables* are the tables (by default the
    ``pyfly_outbox_*`` ones). With *create_tables* false, :meth:`start` only checks them. *notify_channel*,
    on PostgreSQL, is the channel a publish notifies (``NOTIFY``, sent when the publishing unit commits).
    *clock* gives the current UTC instant: the nodes compare it with ``available_at``, so keep their clocks
    synchronized (NTP) well within ``claim_timeout``.
    """

    def __init__(
        self,
        datasource: object = None,
        *,
        tables: OutboxTables | None = None,
        create_tables: bool = True,
        notify_channel: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = datasource
        self._tables = tables
        self._create_tables = create_tables
        self._notify_channel = notify_channel
        self._clock = clock or (lambda: datetime.now(UTC))
        self._skip_locked: bool | None = None

    @property
    def tables(self) -> OutboxTables:
        """The outbox's tables."""
        if self._tables is None:
            self._tables = OutboxTables.named()
        return self._tables

    @property
    def datasource(self) -> object:
        """Where the tables live, as given."""
        return self._target

    @property
    def creates_tables(self) -> bool:
        """Whether :meth:`start` creates missing tables (otherwise it only checks them)."""
        return self._create_tables

    def use_datasource(self, datasource: object) -> None:
        """Put the outbox on *datasource* (before it is used: a bus given a URL resolves it at start)."""
        self._target = datasource
        self._skip_locked = None

    @property
    def notify_channel(self) -> str | None:
        """The PostgreSQL channel a publish notifies, if any."""
        return self._notify_channel

    def now(self) -> datetime:
        """The outbox's clock."""
        return self._clock()

    def manager(self) -> TransactionManager:
        """The transaction manager of the outbox's datasource, resolved now."""
        from pyfly.data.transaction import resolve_manager

        return resolve_manager(self._target)

    def engine(self) -> AsyncEngine:
        """The engine of the outbox's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    def dialect(self) -> str:
        """The backend of the outbox's datasource (``postgresql``, ``sqlite``, ``mysql``, ``mariadb``...)."""
        from pyfly.data.relational.upsert import backend_name

        return backend_name(self.engine())

    async def start(self) -> None:
        """Create the tables when allowed, and check them
        (:func:`~pyfly.data.relational.framework_schema.ensure_tables`)."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, *self.tables.all(), create=self._create_tables)

    def _gap_locking(self) -> bool:
        """Whether the backend takes gap locks under its default isolation (InnoDB: MySQL and MariaDB)."""
        return self.dialect() in ("mysql", "mariadb")

    @contextlib.asynccontextmanager
    async def _relay_unit(self, *, single_statement: bool = False) -> AsyncIterator[AsyncSession]:
        """The unit a relay-side statement runs in: the bound unit, or a short one of its own.

        On MySQL and MariaDB a unit of its own reads ``READ COMMITTED``: InnoDB then locks the rows a claim or a
        delete matches and not the gaps between them, so a publisher inserting a delivery row never waits for a
        relay's claim (under ``REPEATABLE READ`` the two deadlocked, and the publisher's unit could be the one
        rolled back)."""
        from pyfly.data.transaction import Isolation, TransactionTemplate, infrastructure_unit

        if self._gap_locking():
            template = TransactionTemplate(self._target, isolation=Isolation.READ_COMMITTED)
            async with template.transaction() as unit:
                assert unit is not None
                yield unit.resource
            return
        async with infrastructure_unit(self._target, single_statement=single_statement) as session:
            yield session

    # -- writing --------------------------------------------------------------------------------------------

    async def append(self, envelope: EventEnvelope, *, groups: Sequence[str] | None = None) -> int:
        """Write *envelope* in the unit of work bound for the outbox's datasource (or a short unit of its own)
        and return its outbox id.

        It is owed to every consumer group registered for its destination, or to *groups* when given (then no
        registration is consulted). On PostgreSQL the publish is one statement, ``NOTIFY`` included.
        """
        from pyfly.data.transaction import infrastructure_unit

        now = self._clock()
        values = {
            "event_id": envelope.event_id,
            "destination": envelope.destination,
            "event_type": envelope.event_type,
            "payload": encode_json(envelope.payload),
            "headers": encode_json(envelope.headers),
            "created_at": now,
        }
        postgresql = self.dialect() == "postgresql"
        single = postgresql and groups is None
        async with infrastructure_unit(self._target, single_statement=single) as session:
            if single:
                return await self._append_on_postgresql(session, values)
            return await self._append(session, values, groups, notify=postgresql)

    async def _append(
        self, session: AsyncSession, values: dict[str, Any], groups: Sequence[str] | None, *, notify: bool
    ) -> int:
        from sqlalchemy import BigInteger, Integer, func, insert, literal, select

        from pyfly.data.relational.framework_schema import UtcTimestamp

        tables = self.tables
        result: Any = await session.execute(insert(tables.events).values(values))
        outbox_id = int(result.inserted_primary_key[0])
        deliveries, consumers = tables.deliveries, tables.consumers
        destinations = [values["destination"], EVERY_DESTINATION]
        if groups is None and self._gap_locking():
            # InnoDB's INSERT ... SELECT takes shared next-key locks on what it reads: read the groups with a
            # plain (non-locking) read instead, so a publish never holds locks on the consumer table.
            query = select(consumers.c.consumer_group).where(consumers.c.destination.in_(destinations))
            groups = list(dict.fromkeys((await session.execute(query)).scalars()))
        if groups is None:
            owed = (
                select(
                    consumers.c.consumer_group,
                    literal(outbox_id, BigInteger()),
                    literal(values["created_at"], UtcTimestamp()),
                    literal(0, Integer()),
                )
                .where(consumers.c.destination.in_(destinations))
                .distinct()
            )
            await session.execute(
                insert(deliveries).from_select(["consumer_group", "outbox_id", "available_at", "attempts"], owed)
            )
        elif groups:
            await session.execute(
                insert(deliveries).values(
                    [
                        {
                            "consumer_group": group,
                            "outbox_id": outbox_id,
                            "available_at": values["created_at"],
                            "attempts": 0,
                        }
                        for group in dict.fromkeys(groups)
                    ]
                )
            )
        if notify and self._notify_channel:
            await session.execute(select(func.pg_notify(self._notify_channel, "")))
        return outbox_id

    async def _append_on_postgresql(self, session: AsyncSession, values: dict[str, Any]) -> int:
        from sqlalchemy import bindparam, text

        from pyfly.data.relational.framework_schema import UtcTimestamp

        tables = self.tables
        preparer = session.get_bind().dialect.identifier_preparer
        events = preparer.format_table(tables.events)
        deliveries = preparer.format_table(tables.deliveries)
        consumers = preparer.format_table(tables.consumers)
        notify = ", pg_notify(:channel, '')" if self._notify_channel else ""
        statement = text(
            f"WITH e AS (INSERT INTO {events} (event_id, destination, event_type, payload, headers, created_at) "
            "VALUES (:event_id, :destination, :event_type, :payload, :headers, :created_at) RETURNING id), "
            f"d AS (INSERT INTO {deliveries} (consumer_group, outbox_id, available_at, attempts) "
            f"SELECT DISTINCT c.consumer_group, e.id, :created_at, 0 FROM {consumers} c CROSS JOIN e "
            "WHERE c.destination IN (:destination, :every)) "
            f"SELECT e.id{notify} FROM e"
        ).bindparams(bindparam("created_at", type_=UtcTimestamp()))
        parameters = {**values, "every": EVERY_DESTINATION}
        if self._notify_channel:
            parameters["channel"] = self._notify_channel
        row = (await session.execute(statement, parameters)).first()
        assert row is not None
        return int(row[0])

    # -- consumer groups ------------------------------------------------------------------------------------

    async def register(
        self,
        group: str,
        destinations: Sequence[str] | None,
        *,
        start: StartPosition | str = StartPosition.LATEST,
    ) -> bool:
        """Register consumer group *group* for *destinations* (``None``: every destination); returns whether
        the group was new.

        The group's destinations become exactly these: the ones it no longer lists stop being owed to it. A new
        group starts at *start*: with the events published from now on, or (``earliest``) with every event the
        outbox holds for its destinations as well.
        """
        from sqlalchemy import delete, select

        from pyfly.data.relational.upsert import insert_if_absent

        wanted = sorted(set(destinations)) if destinations else [EVERY_DESTINATION]
        consumers = self.tables.consumers
        now = self._clock()
        async with self._relay_unit() as session:
            existing: set[str] = set(
                (
                    await session.execute(select(consumers.c.destination).where(consumers.c.consumer_group == group))
                ).scalars()
            )
            stale = existing - set(wanted)
            if stale:
                await session.execute(
                    delete(consumers).where(consumers.c.consumer_group == group, consumers.c.destination.in_(stale))
                )
            for destination in wanted:
                if destination not in existing:
                    await insert_if_absent(
                        session,
                        consumers,
                        {"consumer_group": group, "destination": destination, "registered_at": now},
                        key=("consumer_group", "destination"),
                    )
        new_group = not existing
        if new_group and StartPosition.of(start) is StartPosition.EARLIEST:
            await self._backfill(group, wanted)
        return new_group

    async def _backfill(self, group: str, destinations: Sequence[str]) -> None:
        """Owe *group* every event the outbox holds for *destinations* that it is not owed yet."""
        from sqlalchemy import Integer, exists, insert, literal, select

        from pyfly.data.relational.framework_schema import UtcTimestamp

        events, deliveries = self.tables.events, self.tables.deliveries
        source = select(
            literal(group, deliveries.c.consumer_group.type),
            events.c.id,
            literal(self._clock(), UtcTimestamp()),
            literal(0, Integer()),
        ).where(~exists().where(deliveries.c.consumer_group == group, deliveries.c.outbox_id == events.c.id))
        if EVERY_DESTINATION not in destinations:
            source = source.where(events.c.destination.in_(list(destinations)))
        async with self._relay_unit() as session:
            await session.execute(
                insert(deliveries).from_select(["consumer_group", "outbox_id", "available_at", "attempts"], source)
            )

    async def unregister(self, group: str) -> int:
        """Remove consumer group *group*: nothing is owed to it any more (the deliveries it had not made are
        deleted too). Returns how many deliveries were dropped."""
        from sqlalchemy import delete

        tables = self.tables
        async with self._relay_unit() as session:
            await session.execute(delete(tables.consumers).where(tables.consumers.c.consumer_group == group))
            result = await session.execute(delete(tables.deliveries).where(tables.deliveries.c.consumer_group == group))
        return int(getattr(result, "rowcount", 0) or 0)

    # -- claiming and settling ------------------------------------------------------------------------------

    async def claim(self, group: str, *, limit: int, lease: timedelta, owner: str) -> list[Delivery]:
        """Claim up to *limit* deliveries owed to *group* whose time has come, for *lease*, in one short unit
        of their own (see the module documentation); returns them in publication order."""

        now = self._clock()
        token = f"{owner}/{uuid.uuid4().hex[:12]}"
        postgresql = self.dialect() == "postgresql"
        async with self._relay_unit(single_statement=postgresql) as session:
            if postgresql:
                rows = await self._claim_on_postgresql(session, group, limit, now + lease, now, token)
            else:
                rows = await self._claim(session, group, limit, now + lease, now, token)
        claimed: list[Delivery] = []
        orphans: list[int] = []
        for row in rows:
            if row.event_id is None:
                orphans.append(int(row.outbox_id))  # the event was pruned under a backfilled delivery
                continue
            claimed.append(
                Delivery(
                    outbox_id=int(row.outbox_id),
                    group=group,
                    envelope=self._envelope(row),
                    attempts=int(row.attempts),
                    done=frozenset(json.loads(row.done)) if row.done else frozenset(),
                    token=token,
                    last_error=row.last_error,
                )
            )
        if orphans:
            await self._drop(group, orphans, token)
        return claimed

    async def _claim(
        self, session: AsyncSession, group: str, limit: int, until: datetime, now: datetime, token: str
    ) -> Sequence[Any]:
        from sqlalchemy import select, update

        deliveries, events = self.tables.deliveries, self.tables.events
        candidates = (
            select(deliveries.c.outbox_id)
            .where(deliveries.c.consumer_group == group, deliveries.c.available_at <= now)
            .order_by(deliveries.c.available_at, deliveries.c.outbox_id)
            .limit(limit)
        )
        if await self._skips_locked_rows(session):
            candidates = candidates.with_for_update(skip_locked=True)
        ids: list[int] = list((await session.execute(candidates)).scalars())
        if not ids:
            return []
        await session.execute(
            update(deliveries)
            .where(
                deliveries.c.consumer_group == group,
                deliveries.c.outbox_id.in_(ids),
                deliveries.c.available_at <= now,
            )
            .values(available_at=until, attempts=deliveries.c.attempts + 1, claimed_by=token)
        )
        claimed = (
            select(
                deliveries.c.outbox_id,
                deliveries.c.attempts,
                deliveries.c.done,
                deliveries.c.last_error,
                events.c.event_id,
                events.c.destination,
                events.c.event_type,
                events.c.payload,
                events.c.headers,
                events.c.created_at,
            )
            .select_from(deliveries.outerjoin(events, events.c.id == deliveries.c.outbox_id))
            .where(deliveries.c.consumer_group == group, deliveries.c.claimed_by == token)
            .order_by(deliveries.c.outbox_id)
        )
        return (await session.execute(claimed)).all()

    async def _claim_on_postgresql(
        self, session: AsyncSession, group: str, limit: int, until: datetime, now: datetime, token: str
    ) -> Sequence[Any]:
        from sqlalchemy import bindparam, text

        from pyfly.data.relational.framework_schema import UtcTimestamp

        tables = self.tables
        preparer = session.get_bind().dialect.identifier_preparer
        deliveries = preparer.format_table(tables.deliveries)
        events = preparer.format_table(tables.events)
        statement = text(
            f"WITH c AS (SELECT consumer_group, outbox_id FROM {deliveries} "
            "WHERE consumer_group = :group AND available_at <= :now "
            "ORDER BY available_at, outbox_id LIMIT :limit FOR UPDATE SKIP LOCKED), "
            f"u AS (UPDATE {deliveries} AS d SET available_at = :until, attempts = d.attempts + 1, "
            "claimed_by = :token FROM c WHERE d.consumer_group = c.consumer_group AND d.outbox_id = c.outbox_id "
            "RETURNING d.outbox_id, d.attempts, d.done, d.last_error) "
            "SELECT u.outbox_id, u.attempts, u.done, u.last_error, e.event_id, e.destination, e.event_type, "
            f"e.payload, e.headers, e.created_at FROM u LEFT JOIN {events} e ON e.id = u.outbox_id "
            "ORDER BY u.outbox_id"
        ).bindparams(
            bindparam("now", type_=UtcTimestamp()),
            bindparam("until", type_=UtcTimestamp()),
        )
        result = await session.execute(
            statement, {"group": group, "now": now, "until": until, "limit": limit, "token": token}
        )
        return result.all()

    async def _skips_locked_rows(self, session: AsyncSession) -> bool:
        """Whether the backend has ``FOR UPDATE SKIP LOCKED``: PostgreSQL, MySQL 8.0.1+, MariaDB 10.6+."""
        if self._skip_locked is None:
            dialect = session.get_bind().dialect
            version = tuple(getattr(dialect, "server_version_info", None) or ())
            name = dialect.name
            if name == "postgresql":
                self._skip_locked = True
            elif name in ("mysql", "mariadb"):
                mariadb = name == "mariadb" or bool(getattr(dialect, "is_mariadb", False))
                self._skip_locked = version >= ((10, 6) if mariadb else (8, 0, 1))
            else:
                self._skip_locked = False
        return self._skip_locked

    def _envelope(self, row: Any) -> EventEnvelope:
        created = row.created_at
        if isinstance(created, str):  # a raw text() read on a backend without a native timestamp
            created = datetime.fromisoformat(created)
        if isinstance(created, datetime) and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return EventEnvelope(
            event_type=row.event_type,
            payload=json.loads(row.payload),
            destination=row.destination,
            event_id=row.event_id,
            timestamp=created,
            headers=json.loads(row.headers) if row.headers else {},
        )

    async def settle(
        self,
        delivery: Delivery,
        *,
        done: Iterable[str] = (),
        retry_at: datetime | None = None,
        error: str | None = None,
        dead: Sequence[tuple[str, BaseException]] = (),
    ) -> bool:
        """Record what became of a claimed delivery: the subscriptions that handled it (*done*), the ones that
        go to the dead letters (*dead*: subscription key and failure), and then either the next attempt at
        *retry_at* (of the others) or the end of the delivery. One unit of its own. Returns ``False``, and
        records nothing, when another relay claimed the delivery since (its lease had ended)."""
        from sqlalchemy import delete, insert, update

        deliveries = self.tables.deliveries
        mine = (
            deliveries.c.consumer_group == delivery.group,
            deliveries.c.outbox_id == delivery.outbox_id,
            deliveries.c.claimed_by == delivery.token,
        )
        if retry_at is None:
            statement: Any = delete(deliveries).where(*mine)
        else:
            statement = (
                update(deliveries)
                .where(*mine)
                .values(
                    available_at=retry_at,
                    claimed_by=None,
                    done=encode_json(sorted(set(done))),
                    last_error=_truncated(error) if error else None,
                )
            )
        try:
            async with self._relay_unit(single_statement=not dead) as session:
                if dead:
                    failed_at = self._clock()
                    await session.execute(
                        insert(self.tables.dead_letters).values(
                            [self._dead_letter_row(delivery, key, failure, failed_at) for key, failure in dead]
                        )
                    )
                result = await session.execute(statement)
                if int(getattr(result, "rowcount", 0) or 0) == 0:
                    raise _LeaseLost
        except _LeaseLost:
            _logger.warning(
                "outbox_delivery_claimed_again",
                extra={
                    "group": delivery.group,
                    "outbox_id": delivery.outbox_id,
                    "event_id": delivery.envelope.event_id,
                },
            )
            return False
        return True

    def _dead_letter_row(
        self, delivery: Delivery, subscription: str, failure: BaseException, failed_at: datetime
    ) -> dict[str, Any]:
        envelope = delivery.envelope
        return {
            "id": str(uuid.uuid4()),
            "consumer_group": delivery.group,
            "subscription": subscription,
            "event_id": envelope.event_id,
            "destination": envelope.destination,
            "event_type": envelope.event_type,
            "payload": encode_json(envelope.payload),
            "headers": encode_json(envelope.headers),
            "occurred_at": envelope.timestamp,
            "error_type": type(failure).__name__[:255],
            "error_message": _truncated(str(failure)),
            "attempts": delivery.attempts,
            "failed_at": failed_at,
        }

    async def release(self, deliveries: Sequence[Delivery]) -> int:
        """Give claimed deliveries back unattempted (a relay that stops): they may be claimed again at once, and
        the claim does not count as an attempt. Returns how many were given back."""
        if not deliveries:
            return 0
        from sqlalchemy import update

        table = self.tables.deliveries
        released = 0
        for group, token in {(delivery.group, delivery.token) for delivery in deliveries}:
            ids = [d.outbox_id for d in deliveries if d.group == group and d.token == token]
            async with self._relay_unit(single_statement=True) as session:
                result = await session.execute(
                    update(table)
                    .where(table.c.consumer_group == group, table.c.outbox_id.in_(ids), table.c.claimed_by == token)
                    .values(available_at=self._clock(), claimed_by=None, attempts=table.c.attempts - 1)
                )
            released += int(getattr(result, "rowcount", 0) or 0)
        return released

    async def _drop(self, group: str, ids: Sequence[int], token: str) -> None:
        from sqlalchemy import delete

        table = self.tables.deliveries
        async with self._relay_unit(single_statement=True) as session:
            await session.execute(
                delete(table).where(
                    table.c.consumer_group == group, table.c.outbox_id.in_(list(ids)), table.c.claimed_by == token
                )
            )

    # -- reading ----------------------------------------------------------------------------------------------

    async def pending(self, group: str, *, limit: int = 1000) -> list[PendingDelivery]:
        """The deliveries still owed to *group* (claimed ones included), oldest first."""
        from sqlalchemy import select

        from pyfly.data.transaction import infrastructure_unit

        deliveries, events = self.tables.deliveries, self.tables.events
        query = (
            select(
                deliveries.c.outbox_id,
                deliveries.c.attempts,
                deliveries.c.available_at,
                deliveries.c.last_error,
                events.c.event_id,
                events.c.destination,
                events.c.event_type,
                events.c.payload,
                events.c.headers,
                events.c.created_at,
            )
            .select_from(deliveries.join(events, events.c.id == deliveries.c.outbox_id))
            .where(deliveries.c.consumer_group == group)
            .order_by(deliveries.c.outbox_id)
            .limit(limit)
        )
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (await session.execute(query)).all()
        return [
            PendingDelivery(
                outbox_id=int(row.outbox_id),
                envelope=self._envelope(row),
                attempts=int(row.attempts),
                available_at=row.available_at,
                last_error=row.last_error,
            )
            for row in rows
        ]

    async def dead_letters(self, group: str | None = None, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        """The dead letters (of *group*, or of every group), most recent first."""
        from pyfly.eda.dlq import SqlEdaDeadLetterStore

        store = SqlEdaDeadLetterStore(self._target, table=self.tables.dead_letters, create_table=False)
        return await store.list(limit=limit, group=group)

    # -- retention --------------------------------------------------------------------------------------------

    async def prune(self, retention: Retention) -> PruneResult:
        """Delete what *retention* lets go, in batches of short units (see :class:`Retention`)."""
        from sqlalchemy import delete, exists, func, select

        events, deliveries = self.tables.events, self.tables.deliveries
        now = self._clock()
        delivered = expired = undelivered = 0
        if retention.delivered is not None:
            cutoff = now - retention.delivered
            while True:
                # Delivery rows are written in the unit that writes their event, so an event that is visible with
                # no delivery row left has been handled by every group it was owed to.
                candidates = (
                    select(events.c.id)
                    .where(events.c.created_at < cutoff, ~exists().where(deliveries.c.outbox_id == events.c.id))
                    .order_by(events.c.id)
                    .limit(retention.batch_size)
                )
                async with self._relay_unit() as session:
                    ids: list[int] = list((await session.execute(candidates)).scalars())
                    if ids:
                        await session.execute(delete(events).where(events.c.id.in_(ids)))
                delivered += len(ids)
                if len(ids) < retention.batch_size:
                    break
        if retention.max_age is not None:
            cutoff = now - retention.max_age
            while True:
                candidates = (
                    select(events.c.id)
                    .where(events.c.created_at < cutoff)
                    .order_by(events.c.id)
                    .limit(retention.batch_size)
                )
                async with self._relay_unit() as session:
                    ids = list((await session.execute(candidates)).scalars())
                    dropped = 0
                    if ids:
                        dropped = int(
                            (
                                await session.execute(
                                    select(func.count()).select_from(deliveries).where(deliveries.c.outbox_id.in_(ids))
                                )
                            ).scalar_one()
                        )
                        await session.execute(delete(deliveries).where(deliveries.c.outbox_id.in_(ids)))
                        await session.execute(delete(events).where(events.c.id.in_(ids)))
                expired += len(ids)
                undelivered += dropped
                if len(ids) < retention.batch_size:
                    break
            if undelivered:
                _logger.warning(
                    "outbox_undelivered_events_expired",
                    extra={"events": expired, "deliveries": undelivered, "max_age": str(retention.max_age)},
                )
        return PruneResult(delivered=delivered, expired=expired, undelivered=undelivered)


# ---------------------------------------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Subscription:
    """A handler subscribed to the event types matching *pattern* (``fnmatch``). *key* names it in the
    delivery rows and the dead letters, the same on every node and after a restart."""

    key: str
    pattern: str
    handler: EventHandler


def subscription_key(pattern: str, handler: object) -> str:
    """``<pattern> <module>.<qualified name>`` of *handler* (a bound method is named after its class), so the
    key is the same in every process of a consumer group and across restarts."""
    from pyfly.messaging.listener_container import listener_target

    target = listener_target(handler)
    owner = getattr(target, "__self__", None)
    function = getattr(target, "__func__", target)
    name = getattr(function, "__name__", None)
    if owner is not None and not isinstance(owner, type) and name:
        module, qualname = type(owner).__module__, f"{type(owner).__qualname__}.{name}"
    else:
        module = getattr(function, "__module__", None) or type(target).__module__
        qualname = getattr(function, "__qualname__", None) or type(target).__qualname__
    return f"{pattern} {module}.{qualname}"


# ---------------------------------------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------------------------------------


class RelayState(enum.Enum):
    """Where a relay is in its lifecycle."""

    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class RelayCounters:
    """What a relay did since it was built: deliveries completed, failed attempts, dead letters written, dead
    letters an external store refused, and rounds that failed."""

    delivered: int = 0
    failures: int = 0
    dead_lettered: int = 0
    dead_letter_store_failures: int = 0
    failed_rounds: int = 0
    pruned: int = 0


RoundHook = Callable[[], Awaitable[None]]


class OutboxRelay:
    """Hands the events an :class:`Outbox` owes consumer group *group* to its subscriptions (see the module
    documentation).

    - *destinations*: what the group consumes (``None``: every destination); with *register* the relay
      registers the group for them when its first subscription arrives, starting at *start_position*.
      Without, deliveries reach it only when the outbox is told to owe them to *group*;
    - *retry* and *error_strategy*: what a failure leads to (the default policy: 5 attempts, exponential
      back-off from 1 s to 30 s, then the dead-letter table); *dead_letter_store*, when given, receives the
      dead letters instead of the outbox's table;
    - *settings*: the listener container settings (``pyfly.eda.listener.*``): a handler runs in the unit of
      work they and its ``@transactional`` give it, unless *transactional* is false;
    - *poll_interval*, *batch_size*, *claim_timeout* (the lease) and *handler_timeout* (``None``: none), in
      seconds; *retention* (``None``: the relay prunes nothing); *owner* identifies this relay in the claims.
    """

    #: The relay stops before any ``@pre_destroy``, draining the delivery in flight.
    phase = CONSUMER_PHASE

    def __init__(
        self,
        outbox: Outbox,
        *,
        group: str,
        destinations: Sequence[str] | None = None,
        register: bool = True,
        start_position: StartPosition | str = StartPosition.LATEST,
        retry: RetryPolicy | None = None,
        error_strategy: ErrorStrategy = ErrorStrategy.DEAD_LETTER,
        settings: ListenerContainerSettings | None = None,
        transactional: bool = True,
        poll_interval: float = 5.0,
        batch_size: int = 100,
        claim_timeout: float = 300.0,
        handler_timeout: float | None = 60.0,
        retention: Retention | None = None,
        dead_letter_store: EdaDeadLetterStore | None = None,
        owner: str | None = None,
        name: str | None = None,
    ) -> None:
        from pyfly.messaging.listener_container import ListenerContainerSettings, ListenerInvoker

        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        if handler_timeout is not None and handler_timeout <= 0:
            raise ValueError(f"handler_timeout must be positive, got {handler_timeout}")
        if claim_timeout <= (handler_timeout or 0):
            raise ValueError(
                f"claim_timeout ({claim_timeout} s) must be longer than handler_timeout ({handler_timeout} s): a "
                "delivery claimed again while its handler still runs is handled twice at once"
            )
        self._outbox = outbox
        self._group = group
        self._destinations = list(destinations) if destinations else None
        self._register = register
        self._start_position = StartPosition.of(start_position)
        self._settings = settings or ListenerContainerSettings()
        self._retry = retry or self._settings.retry
        self._error_strategy = error_strategy
        self._invoker: ListenerInvoker | None = (
            ListenerInvoker(self._settings, name=name or f"outbox {group}") if transactional else None
        )
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._claim_timeout = timedelta(seconds=claim_timeout)
        self._handler_timeout = handler_timeout
        self._retention = retention
        self._dead_letter_store = dead_letter_store
        if owner is None:
            from pyfly.scheduling.adapters.lease_lock import default_owner

            owner = default_owner()
        self._owner = owner[:200]
        self._name = name or f"outbox-relay {group}"
        self._subscriptions: list[Subscription] = []
        self._hooks: list[RoundHook] = []
        self._state = RelayState.NEW
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._registered = False
        self._next_prune: datetime | None = None
        self._next_due: datetime | None = None
        self._last_error: str | None = None
        self.counters = RelayCounters()

    # -- configuration --------------------------------------------------------------------------------------

    @property
    def group(self) -> str:
        """The consumer group the relay delivers for."""
        return self._group

    @property
    def destinations(self) -> list[str] | None:
        """The destinations the group consumes (``None``: every one)."""
        return None if self._destinations is None else list(self._destinations)

    @property
    def outbox(self) -> Outbox:
        """The outbox the relay reads."""
        return self._outbox

    @property
    def poll_interval(self) -> float:
        """Seconds between two polls of an idle relay."""
        return self._poll_interval

    @property
    def subscriptions(self) -> list[Subscription]:
        """The subscriptions, in the order they were made."""
        return list(self._subscriptions)

    @property
    def state(self) -> RelayState:
        """Where the relay is in its lifecycle."""
        return self._state

    @property
    def running(self) -> bool:
        """Whether the relay is started."""
        return self._state is RelayState.RUNNING

    @property
    def last_error(self) -> str | None:
        """The failure of the relay's last failed round, if any."""
        return self._last_error

    def subscribe(self, pattern: str, handler: EventHandler) -> Subscription:
        """Subscribe *handler* to the event types matching *pattern*; the relay starts claiming once it has a
        subscription (events published before wait for it)."""
        key = subscription_key(pattern, handler)
        taken = {subscription.key for subscription in self._subscriptions}
        if key in taken:
            number = 2
            while f"{key}#{number}" in taken:
                number += 1
            key = f"{key}#{number}"
        subscription = Subscription(key, pattern, handler)
        self._subscriptions.append(subscription)
        self._wake.set()
        return subscription

    def add_round_hook(self, hook: RoundHook) -> None:
        """Await *hook* before every round (a bus keeps its wake-up connection alive there)."""
        self._hooks.append(hook)

    def wake(self) -> None:
        """Make the relay claim now instead of at its next poll."""
        self._wake.set()

    def wake_after(self, seconds: float) -> None:
        """Make the relay's next round come no later than *seconds* from now."""
        due = self._outbox.now() + timedelta(seconds=max(0.0, seconds))
        if self._next_due is None or due < self._next_due:
            self._next_due = due

    # -- lifecycle ----------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the relay in a detached task. Idempotent; a stopped relay can be started again."""
        async with self._lock:
            if self._state in (RelayState.RUNNING, RelayState.STARTING):
                return
            from pyfly.data.transaction import detached

            self._state = RelayState.STARTING
            self._wake = asyncio.Event()
            self._wake.set()
            self._registered = False
            self._task = detached(self._run(), name=self._name)
            self._state = RelayState.RUNNING

    async def stop(self) -> None:
        """Stop the relay: the delivery in flight finishes (for the listener shutdown timeout, then it is
        cancelled), and the deliveries it had claimed and not started are given back. Idempotent."""
        async with self._lock:
            task = self._task
            if self._state is not RelayState.RUNNING or task is None:
                self._state = RelayState.STOPPED if self._state is not RelayState.NEW else self._state
                return
            self._state = RelayState.STOPPING
            self._wake.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._settings.shutdown_timeout)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            except Exception:  # noqa: BLE001 — the loop logs its own failures; stopping goes on
                _logger.debug("outbox_relay_stop_failed", exc_info=True)
            finally:
                self._task = None
                self._state = RelayState.STOPPED

    # -- the loop -------------------------------------------------------------------------------------------------

    def _stopping(self) -> bool:
        return self._state is not RelayState.RUNNING

    async def _run(self) -> None:
        failures = 0
        while not self._stopping():
            if not self._subscriptions:
                await self._wait(None)
                continue
            try:
                for hook in list(self._hooks):
                    await hook()
                handled = await self.run_once()
                await self._maybe_prune()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — a failed round is logged and tried again
                failures += 1
                self.counters.failed_rounds += 1
                self._last_error = describe_error(error)
                _logger.exception("outbox_relay_round_failed", extra={"group": self._group, "relay": self._name})
                await self._wait(min(self._poll_interval, 0.5 * 2 ** min(failures, 6)))
                continue
            failures = 0
            self._last_error = None
            if handled >= self._batch_size:
                continue  # a full batch: there may be more at once
            await self._wait(self._idle_timeout())

    def _idle_timeout(self) -> float:
        timeout = self._poll_interval
        if self._next_due is not None:
            remaining = (self._next_due - self._outbox.now()).total_seconds()
            self._next_due = None
            timeout = max(0.01, min(timeout, remaining))
        return timeout

    async def _wait(self, timeout: float | None) -> None:
        if self._stopping():
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        self._wake.clear()

    async def run_once(self) -> int:
        """Claim one batch and deliver it; returns how many deliveries were claimed. The loop calls it; a test
        or a maintenance script may too, on a relay that is not running (a registering relay then registers
        its group first)."""
        if not self._subscriptions:
            return 0
        await self.register()
        return await self._round(in_loop=self._task is not None and asyncio.current_task() is self._task)

    async def register(self) -> None:
        """Register the relay's consumer group for its destinations, once (a registering relay does it before
        its first claim, and a bus when it starts with subscriptions): from then on every event published to
        them is owed to the group, whether or not a relay runs."""
        if self._register and not self._registered:
            await self._outbox.register(self._group, self._destinations, start=self._start_position)
            self._registered = True

    async def _round(self, *, in_loop: bool) -> int:
        claimed = await self._outbox.claim(
            self._group, limit=self._batch_size, lease=self._claim_timeout, owner=self._owner
        )
        deadline = self._outbox.now() + self._claim_timeout
        for index, delivery in enumerate(claimed):
            if (in_loop and self._stopping()) or self._outbox.now() + self._budget(delivery) > deadline:
                # Stopping, or the lease would end under the next handler: give the rest back unattempted.
                await self._outbox.release(claimed[index:])
                break
            await self._deliver(delivery)
        return len(claimed)

    def _budget(self, delivery: Delivery) -> timedelta:
        if self._handler_timeout is None:
            return timedelta(0)
        pending = sum(1 for subscription in self._matching(delivery))
        return timedelta(seconds=self._handler_timeout * max(1, pending))

    def _matching(self, delivery: Delivery) -> list[Subscription]:
        event_type = delivery.envelope.event_type
        return [
            subscription
            for subscription in self._subscriptions
            if subscription.key not in delivery.done and fnmatch.fnmatch(event_type, subscription.pattern)
        ]

    async def _deliver(self, delivery: Delivery) -> None:
        done = set(delivery.done)
        failures: list[tuple[Subscription, BaseException]] = []
        for subscription in self._matching(delivery):
            try:
                await self._invoke(subscription, delivery.envelope)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — a handler's failure is the delivery's outcome
                failures.append((subscription, error))
            else:
                done.add(subscription.key)
        if not failures:
            if await self._outbox.settle(delivery):
                self.counters.delivered += 1
            return
        self.counters.failures += len(failures)
        await self._handle_failures(delivery, done, failures)

    async def _invoke(self, subscription: Subscription, envelope: EventEnvelope) -> None:
        from pyfly.messaging.listener_container import DeliveryState

        call = functools.partial(subscription.handler, envelope)
        if self._invoker is None:
            operation: Awaitable[None] = call()
        else:
            operation = self._invoker.invoke(call, DeliveryState(), listeners=(subscription.handler,))
        if self._handler_timeout is None:
            await operation
            return
        async with asyncio.timeout(self._handler_timeout):
            await operation

    async def _handle_failures(
        self, delivery: Delivery, done: set[str], failures: list[tuple[Subscription, BaseException]]
    ) -> None:
        """Settle a delivery some subscriptions failed: under the error strategy, each failure is attempted
        again after its back-off, goes to the dead letters, or is let go."""
        strategy = self._error_strategy
        for subscription, error in failures:
            level = logging.DEBUG if strategy is ErrorStrategy.IGNORE else logging.WARNING
            _logger.log(
                level,
                "outbox_delivery_failed",
                extra={
                    "group": self._group,
                    "subscription": subscription.key,
                    "event_id": delivery.envelope.event_id,
                    "event_type": delivery.envelope.event_type,
                    "attempt": delivery.attempts,
                    "error": describe_error(error),
                },
                exc_info=(type(error), error, error.__traceback__) if level > logging.DEBUG else None,
            )
        if strategy in (ErrorStrategy.IGNORE, ErrorStrategy.LOG_AND_CONTINUE):
            if await self._outbox.settle(delivery):
                self.counters.delivered += 1
            return
        dead: list[tuple[Subscription, BaseException]] = []
        delays: list[float] = []
        for subscription, error in failures:
            delay = self._retry_delay(subscription, error, delivery.attempts)
            if delay is None:
                dead.append((subscription, error))
            else:
                delays.append(delay)
        if dead and not await self._dead_letter(delivery, dead):
            # The external store refused a dead letter: attempt the delivery again rather than lose it.
            delays.append(1.0)
            dead = []
        retry_at = self._outbox.now() + timedelta(seconds=min(delays)) if delays else None
        settled = await self._outbox.settle(
            delivery,
            done=done | {subscription.key for subscription, _error in dead},
            retry_at=retry_at,
            error=describe_error(failures[-1][1]),
            dead=[] if self._dead_letter_store is not None else [(s.key, error) for s, error in dead],
        )
        if settled:
            self.counters.dead_lettered += len(dead)
        if retry_at is not None and (self._next_due is None or retry_at < self._next_due):
            self._next_due = retry_at

    def _retry_delay(self, subscription: Subscription, error: BaseException, attempt: int) -> float | None:
        """The delay before the next attempt of *subscription*'s failed *attempt*, or ``None`` for the dead
        letters, under the error strategy and the subscription's own listener options."""
        from pyfly.messaging.listener_container import PoisonMessageError, listener_options

        if self._error_strategy is ErrorStrategy.FAIL_FAST or isinstance(error, PoisonMessageError):
            return None
        policy = self._retry.with_options(listener_options(subscription.handler))
        if self._error_strategy is ErrorStrategy.RETRY:
            return max(0.0, policy.backoff.delay_after(attempt))  # retried for as long as it fails
        return policy.retry_delay(error, attempt)

    async def _dead_letter(self, delivery: Delivery, dead: list[tuple[Subscription, BaseException]]) -> bool:
        """Hand the dead letters to the external store, when there is one (the outbox's own table is written in
        the settling unit). Returns whether they were all recorded."""
        store = self._dead_letter_store
        if store is None:
            return True
        for subscription, error in dead:
            entry = EdaDeadLetterEntry(
                event=delivery.envelope,
                error_type=type(error).__name__,
                error_message=_truncated(str(error)),
                attempts=delivery.attempts,
                group=delivery.group,
                subscription=subscription.key,
            )
            try:
                await store.add(entry)
            except Exception:  # noqa: BLE001 — the delivery is attempted again instead
                self.counters.dead_letter_store_failures += 1
                _logger.exception(
                    "outbox_dead_letter_store_failed",
                    extra={"group": delivery.group, "event_id": delivery.envelope.event_id},
                )
                return False
        return True

    async def _maybe_prune(self) -> None:
        retention = self._retention
        if retention is None:
            return
        now = self._outbox.now()
        if self._next_prune is not None and now < self._next_prune:
            return
        self._next_prune = now + retention.interval
        result = await self._outbox.prune(retention)
        self.counters.pruned += result.delivered + result.expired
