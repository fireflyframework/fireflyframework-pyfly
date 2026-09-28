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
"""The transactional outbox on MongoDB: the :class:`~pyfly.eda.ports.outbox.OutboxStore` port on collections of
the application's document database (``pyfly.eda.outbox.store=mongo``).

:class:`MongoOutboxStore` keeps the outbox of a MongoDB application beside its documents, with the guarantees of
the SQL store (:class:`~pyfly.eda.outbox.SqlOutboxStore`; the outbox store contract,
``tests/support/outbox_contract.py``, runs on both): an event is written in the unit of work of the code that
publishes it, so it exists exactly when that unit commits, and the relays claim what each consumer group is owed
by the state of its deliveries, with a lease, never by a cursor. The relay, the ``database`` bus, the forwarding
relay of the transactional publisher and the event-sourcing outbox run on it unchanged.

Five collections of the document database (``pyfly.data.document.database``) hold it, named after a prefix
(``pyfly_outbox`` by default):

- ``pyfly_outbox_events``: one document per event, whose ``_id`` is its outbox id. The payload and the headers
  are kept as JSON text, as the SQL store keeps them, so a consumer gets the same values from either store (an
  instant, a decimal or a UUID in a payload comes back as a string);
- ``pyfly_outbox_deliveries``: what the outbox still owes, one document per consumer group and event: when it is
  due (or when the lease of the claim that holds it ends), the attempts made, the claim that holds it, the
  subscriptions that handled it already and the last failure. A document is deleted once the delivery is made;
- ``pyfly_outbox_consumers``: the consumer groups, and the destinations each consumes (``*``: every one);
- ``pyfly_outbox_dead_letters``: the deliveries a subscription failed to handle on every attempt;
- ``pyfly_outbox_counters``: the counter the outbox ids are taken from.

:meth:`MongoOutboxStore.start` creates their indexes (idempotent: an index that exists is left as it is), and so
the collections: a unique ``(consumer_group, outbox_id)`` and a ``(consumer_group, available_at, outbox_id)``
index on the deliveries (the claim) and one on their ``outbox_id`` (retention), a unique ``(consumer_group,
destination)`` and a ``destination`` index on the consumer groups (an append reads the groups of its
destination), ``created_at`` on the events (retention), and ``(consumer_group, failed_at)`` and ``failed_at`` on the
dead letters. With ``create_indexes=False`` it only checks them, and fails naming the one that is missing.

**A replica set.** An append writes the event and its deliveries in one multi-document transaction: the one of the
unit of work bound for the store's datasource (a ``@transactional`` method of the document datasource, which the
event then commits or rolls back with), or a short one of its own, outside a unit and in a unit that runs no
transaction. The one a single-command repository write opens outside ``@transactional`` runs none: a
``MongoRepository.save`` there writes the document on its own, and the events of its aggregate, appended as its unit
commits, are written with their deliveries in a transaction of the store's own, whole or not at all. MongoDB runs
multi-document transactions on a replica set or a sharded cluster, never on a standalone server, so
:meth:`MongoOutboxStore.start` refuses one (a single-node replica set is enough: ``mongod --replSet rs0``, then
``rs.initiate()``).

**Outbox ids.** An event's outbox id is an integer taken from the counter with one atomic ``findAndModify``
(``$inc``) *outside* the unit's transaction. Two transactions that increment one document conflict: MongoDB aborts
the second at once with a ``WriteConflict``, so a counter inside the transaction would fail a publishing unit
whenever another one ran beside it. The ids grow with each append, as the SQL store's identity column does: a unit
that rolls back leaves a gap, and a unit that commits late leaves a lower id behind higher ones. Neither matters to
the order a group is delivered in: a claim takes the deliveries that are due, in outbox-id order, whatever became
visible when (see :mod:`pyfly.eda.outbox` for what a group's order promises).

**Claims.** A claim reads the group's due deliveries (``available_at`` passed, oldest first), then moves them a
lease ahead with one ``updateMany`` that matches only those still due, and marks them with its token
(``<owner>/<random>``). MongoDB applies an update to each document atomically and matches it again when a
concurrent write changed it, so of two relays that read the same deliveries each takes a delivery the other did
not: none is taken twice, and a delivery whose lease ended is due again. A claim that lost some of what it read
reads on past them (as ``SKIP LOCKED`` would), so the relays of a group share its backlog instead of one of them
coming back empty-handed. Every later write on a claim (complete, settle, extend, release) matches the claim's
token: once another claim took the delivery, it does nothing.

**Units.** Every command runs under the operation guard of its unit, on the unit's ``ClientSession``: the
caller's unit when one is bound for the store's datasource and can hold the work (an append then commits with the
business writes; so do the relay's statements when a caller runs them inside a unit), else a short unit of its
own. Several writes that belong together (an append, a registration, an unregistration, a settle that writes dead
letters, a retention batch) join only a unit that runs a transaction, and otherwise run in a transaction of their
own (one MongoDB aborts for a write conflict with a concurrent transaction is run again, a few times). The work of
one command, which MongoDB applies to each document atomically (a claim, a completion, an extension, a release, a
plain settle), and a read join any unit, or run in one of their own without a transaction. Such a unit reports a
failure after its first command as an unknown outcome, never as a rollback: a write may have stood.

**Sharing.** The store does not own its client: the application's document client belongs to the context that
built it (:data:`~pyfly.data.document.mongodb.initializer.BINDINGS` closes it when the last context using it
stops), and a client given to the store stays the caller's. :meth:`MongoOutboxStore.stop` releases nothing, so the
buses, the transactional publisher and the event-sourcing outbox may share one store and each stop it.

Instants are aware UTC ``datetime`` values, whatever the client's ``tz_aware``; MongoDB keeps them to the
millisecond, and the default clock gives whole milliseconds. The clocks of the nodes are compared with each
other's leases: keep them synchronized (NTP) well within ``claim_timeout``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from bson.codec_options import CodecOptions
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel, ReturnDocument, UpdateOne
from pymongo.errors import PyMongoError

from pyfly.data.document.mongodb.transaction_manager import DOCUMENT, MongoTransactionManager, in_transaction
from pyfly.data.transaction.context import bind_state, current_state, reset_state
from pyfly.data.transaction.errors import IllegalTransactionStateError
from pyfly.data.transaction.registry import resolve_manager
from pyfly.data.transaction.template import AutoUnit
from pyfly.eda.dlq import EdaDeadLetterEntry
from pyfly.eda.outbox import encode_json
from pyfly.eda.ports.outbox import (
    ADDRESSED_DESTINATION_PREFIX,
    EVERY_DESTINATION,
    Delivery,
    PendingDelivery,
    PruneResult,
    Retention,
    StartPosition,
)
from pyfly.eda.types import EventEnvelope

if TYPE_CHECKING:
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.database import AsyncDatabase

    from pyfly.data.transaction.unit_of_work import UnitOfWork

__all__ = ["DEFAULT_DATABASE", "DEFAULT_PREFIX", "MongoOutboxCollections", "MongoOutboxStore"]

_logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_PREFIX = "pyfly_outbox"
"""The prefix of the default outbox collections (``pyfly_outbox_events`` and the others)."""

DEFAULT_DATABASE = "pyfly"
"""The database of a store given a client whose URI names none (``pyfly.data.document.database``'s default)."""

_MAX_ERROR_LENGTH = 4000

_BATCH = 1000
"""How many events one command of a backfill reads."""

_CLAIM_READS = 8
"""How many times one claim reads the due deliveries again when another relay took some of those it read."""

_TRANSIENT_ATTEMPTS = 5
"""How many times a transaction of the store's own runs when MongoDB aborts it for a transient reason."""

_CODEC_OPTIONS: CodecOptions[dict[str, Any]] = CodecOptions(tz_aware=True, tzinfo=UTC)
"""Every instant read back as an aware UTC ``datetime``, whatever the client's own ``tz_aware``."""


def _milliseconds(instant: datetime) -> datetime:
    """*instant* to the millisecond, as MongoDB keeps it."""
    return instant.replace(microsecond=instant.microsecond // 1000 * 1000)


def _now() -> datetime:
    return _milliseconds(datetime.now(UTC))


def _truncated(text: str) -> str:
    return text if len(text) <= _MAX_ERROR_LENGTH else text[: _MAX_ERROR_LENGTH - 1] + "…"


def _transient(error: BaseException | None) -> bool:
    """Whether *error*, or the driver error it was raised from (a translated commit failure), is one MongoDB labels
    ``TransientTransactionError``: the whole transaction may be run again."""
    for _ in range(8):
        if error is None:
            return False
        if isinstance(error, PyMongoError) and error.has_error_label("TransientTransactionError"):
            return True
        error = error.__cause__
    return False


def _by_claim(deliveries: Iterable[Delivery]) -> dict[tuple[str, str], list[Delivery]]:
    """*deliveries* by the claim (group and token) they belong to, in their order."""
    claims: dict[tuple[str, str], list[Delivery]] = {}
    for delivery in deliveries:
        claims.setdefault((delivery.group, delivery.token), []).append(delivery)
    return claims


class _LeaseLost(Exception):
    """The delivery was claimed again by another relay after this one's lease ended: roll back its settling."""


@dataclass(frozen=True)
class MongoOutboxCollections:
    """The names of the five collections of one outbox (see the module documentation)."""

    events: str
    deliveries: str
    consumers: str
    dead_letters: str
    counters: str

    @classmethod
    def named(cls, prefix: str = DEFAULT_PREFIX) -> MongoOutboxCollections:
        """The collections ``<prefix>_events``, ``<prefix>_deliveries``, ``<prefix>_consumers``,
        ``<prefix>_dead_letters`` and ``<prefix>_counters``."""
        return cls(
            events=f"{prefix}_events",
            deliveries=f"{prefix}_deliveries",
            consumers=f"{prefix}_consumers",
            dead_letters=f"{prefix}_dead_letters",
            counters=f"{prefix}_counters",
        )

    def indexes(self) -> dict[str, list[IndexModel]]:
        """The indexes :meth:`MongoOutboxStore.start` creates, by collection (the ``_id`` ones aside)."""
        return {
            self.events: [IndexModel([("created_at", ASCENDING)], name="created_at")],
            self.deliveries: [
                IndexModel(
                    [("consumer_group", ASCENDING), ("outbox_id", ASCENDING)], name="group_outbox_id", unique=True
                ),
                IndexModel(
                    [("consumer_group", ASCENDING), ("available_at", ASCENDING), ("outbox_id", ASCENDING)],
                    name="group_available_at",
                ),
                IndexModel([("outbox_id", ASCENDING)], name="outbox_id"),
            ],
            self.consumers: [
                IndexModel(
                    [("consumer_group", ASCENDING), ("destination", ASCENDING)], name="group_destination", unique=True
                ),
                IndexModel([("destination", ASCENDING)], name="destination"),
            ],
            self.dead_letters: [
                IndexModel([("consumer_group", ASCENDING), ("failed_at", DESCENDING)], name="group_failed_at"),
                IndexModel([("failed_at", DESCENDING)], name="failed_at"),
            ],
        }


class MongoOutboxStore:
    """The :class:`~pyfly.eda.ports.outbox.OutboxStore` on MongoDB: the outbox collections in one database, and the
    commands that write and read them (see the module documentation).

    *datasource* is the document datasource the store's units run on, resolved at each use: its name
    (``document`` by default: the manager the application context registered under it), its
    :class:`~pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager`, or an ``AsyncMongoClient`` (the
    client's manager, :meth:`MongoTransactionManager.for_client
    <pyfly.data.document.mongodb.transaction_manager.MongoTransactionManager.for_client>`). The store reads and writes
    with that manager's client and never closes it. *database* is the database of the collections (by default the
    one the client's URI names, else ``pyfly``), and *collections* their names (the ``pyfly_outbox_*`` ones by
    default). With *create_indexes* false, :meth:`start` only checks the indexes. *clock* gives the current UTC
    instant (by default the system's, to the millisecond).
    """

    def __init__(
        self,
        datasource: str | AsyncMongoClient[Any] | MongoTransactionManager = DOCUMENT,
        *,
        database: str | None = None,
        collections: MongoOutboxCollections | None = None,
        create_indexes: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(datasource, (str, AsyncMongoClient, MongoTransactionManager)):
            raise TypeError(
                "MongoOutboxStore runs on a document datasource: its name, its MongoTransactionManager or its pymongo "
                f"AsyncMongoClient, got {type(datasource).__name__}"
            )
        self._target = datasource
        self._database_name = database
        self._collections = collections or MongoOutboxCollections.named()
        self._create_indexes = create_indexes
        self._clock = clock or _now

    # -- identity -------------------------------------------------------------------------------------------------

    @property
    def datasource(self) -> str | AsyncMongoClient[Any] | MongoTransactionManager:
        """The document datasource the store runs on, as given."""
        return self._target

    @property
    def client(self) -> AsyncMongoClient[Any]:
        """The client the store reads and writes with, resolved now (the store does not own it)."""
        return self.manager().client

    @property
    def database(self) -> str:
        """The database of the outbox collections."""
        if self._database_name is None:
            self._database_name = self.client.get_default_database(default=DEFAULT_DATABASE).name
        return self._database_name

    @property
    def collections(self) -> MongoOutboxCollections:
        """The names of the outbox collections."""
        return self._collections

    @property
    def creates_indexes(self) -> bool:
        """Whether :meth:`start` creates missing indexes (otherwise it only checks them)."""
        return self._create_indexes

    def manager(self) -> MongoTransactionManager:
        """The transaction manager the store's units run on, resolved now: the one it was given, the one of its
        client, or the one registered under its datasource name (raises
        :class:`~pyfly.data.transaction.errors.IllegalTransactionStateError` when that is no document datasource)."""
        target = self._target
        if isinstance(target, MongoTransactionManager):
            return target
        if isinstance(target, AsyncMongoClient):
            return MongoTransactionManager.for_client(target)
        manager = resolve_manager(target)
        if not isinstance(manager, MongoTransactionManager):
            raise IllegalTransactionStateError(
                f"The MongoDB outbox store runs on a document datasource, and datasource {target!r} is served by "
                f"{manager!r}. Name the document datasource (pyfly.data.document.datasource), or keep the outbox "
                "on this one with the SQL store (pyfly.eda.outbox.store=sql).",
                datasource=target,
            )
        return manager

    def now(self) -> datetime:
        """The store's clock."""
        return self._clock()

    def _db(self) -> AsyncDatabase[dict[str, Any]]:
        return self.client.get_database(self.database, codec_options=_CODEC_OPTIONS)

    def _collection(self, name: str) -> AsyncCollection[dict[str, Any]]:
        return self._db().get_collection(name)

    # -- lifecycle ------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Check that the server runs transactions, create the collections' indexes (or, with *create_indexes*
        false, check them) and set the counter past the highest outbox id held. Idempotent."""
        manager = self.manager()
        if not await manager.supports_transactions():
            raise IllegalTransactionStateError(
                f"The MongoDB outbox store needs multi-document transactions, and datasource '{manager.datasource}' "
                "is a standalone MongoDB server: an append writes the event and its deliveries in the publishing "
                "unit's transaction. Run MongoDB as a replica set (a single-node one is enough: mongod --replSet rs0, "
                "then rs.initiate()), or keep the outbox on a relational datasource (pyfly.eda.outbox.store=sql).",
                datasource=manager.datasource,
            )
        for name, indexes in self._collections.indexes().items():
            collection = self._collection(name)
            if self._create_indexes:
                await collection.create_indexes(indexes)
                continue
            present = await collection.index_information()
            for index in indexes:
                wanted = index.document["name"]
                if wanted not in present:
                    raise IllegalTransactionStateError(
                        f"The MongoDB outbox store's index {wanted!r} of collection {name!r} (database "
                        f"{self.database!r}) is missing, and the store was told not to create its indexes. "
                        "Create them (start the store once with index creation on), or let it create them.",
                        datasource=manager.datasource,
                    )
        highest = await self._collection(self._collections.events).find_one({}, {"_id": 1}, sort=[("_id", DESCENDING)])
        await self._collection(self._collections.counters).update_one(
            {"_id": self._collections.events},
            {"$max": {"value": int(highest["_id"]) if highest is not None else 0}},
            upsert=True,
        )

    async def stop(self) -> None:
        """Nothing to release: the store does not own its client (the context that built it, or the caller, does),
        so several buses and publishers may share it and each stop it. Idempotent; a stopped store can be started
        again."""

    async def ping(self) -> None:
        """Run ``ping`` on the store's database, outside the caller's units of work; raises when the server cannot
        be reached (the buses' health indicators call it)."""
        await self._db().command("ping")

    # -- units ----------------------------------------------------------------------------------------------------

    def _joined(self, manager: MongoTransactionManager, *, write: bool) -> UnitOfWork | None:
        """The unit bound for the store's datasource (the running repository scope's, else the transactional one),
        checked usable, and writable when *write*."""
        state = current_state()
        unit = state.scope(manager.datasource) or state.unit(manager.datasource)
        if unit is None:
            return None
        unit.check_usable()
        if write and unit.read_only:
            raise IllegalTransactionStateError(
                f"{unit.describe()} is read-only, and the MongoDB outbox store cannot write in it. Drop read_only=True "
                "from the boundary, or publish in a unit of its own (Propagation.REQUIRES_NEW).",
                datasource=unit.datasource,
            )
        return unit

    @contextlib.asynccontextmanager
    async def _unit(self, *, read_only: bool = False, single: bool = False) -> AsyncIterator[UnitOfWork]:
        """The unit the store's commands run in: the one bound for its datasource (joined) when it can hold them,
        else a short one of its own, with a transaction unless *read_only* or *single* (one command, atomic on each
        document).

        Several writes that belong together join only a unit that runs a transaction. A unit that runs none (the
        one a single-command repository write opens outside ``@transactional``, such as ``MongoRepository.save``,
        whose aggregate's events are appended as it commits) would send them as separate commands, and a failure
        between them would leave an event owed to no group: they run in a transaction of the store's own instead.
        """
        manager = self.manager()
        joined = self._joined(manager, write=not read_only)
        if joined is not None and (read_only or single or in_transaction(joined)):
            token = bind_state(current_state().with_scope(manager.datasource, joined))
            try:
                yield joined
            finally:
                reset_state(token)
            return
        async with AutoUnit(manager, read_only=read_only, autocommit=True if single else None) as unit:
            if not read_only and not in_transaction(unit):
                # Each command stands as it runs: a failure after one is an unknown outcome, not a rollback.
                unit.autocommit = True
            yield unit

    async def _atomically(self, work: Callable[[UnitOfWork], Awaitable[T]]) -> T:
        """Await ``work(unit)`` in the transaction of the unit bound for the store's datasource, else (no unit, or
        one that runs no transaction) in a transaction of its own, run again (a few times) when MongoDB aborts it
        for a transient reason: a write conflict with a concurrent transaction, such as another node registering
        the same group."""
        manager = self.manager()
        joined = self._joined(manager, write=True)
        if joined is not None and in_transaction(joined):
            async with self._unit() as unit:
                return await work(unit)
        for attempt in range(1, _TRANSIENT_ATTEMPTS + 1):
            try:
                async with self._unit() as unit:
                    return await work(unit)
            except Exception as error:
                if attempt == _TRANSIENT_ATTEMPTS or not _transient(error):
                    raise
                _logger.debug("mongo_outbox_transaction_retried", extra={"attempt": attempt})
                await asyncio.sleep(0.01 * 2**attempt)
        raise AssertionError("unreachable")  # pragma: no cover — the loop returns or raises

    # -- writing --------------------------------------------------------------------------------------------------

    async def _next_id(self) -> int:
        """The next outbox id: one atomic ``$inc`` of the counter, outside every transaction (module
        documentation)."""
        counter = await self._collection(self._collections.counters).find_one_and_update(
            {"_id": self._collections.events},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        assert counter is not None
        return int(counter["value"])

    async def append(
        self, envelope: EventEnvelope, *, groups: Sequence[str] | None = None, include: Sequence[str] = ()
    ) -> int:
        """Write *envelope* in the transaction of the unit of work bound for the store's datasource (outside one, or
        in a unit that runs no transaction, in a short transaction of its own) and return its outbox id.

        It is owed to every consumer group registered for its destination, or to *groups* when given (then no
        registration is consulted), and to the groups in *include* as well, whether or not they are registered
        (each group once). The registered groups are the ones the publishing unit's transaction sees: a group
        first registered after that transaction's first read is not owed the event (a group that registers starts
        with the events published after, and a unit that began before straddles that boundary). A bus that
        consumes the destination includes its own group, so its own events are always owed to it.
        """
        now = self._clock()
        included = list(dict.fromkeys(include))
        if groups is not None:
            groups, included = list(dict.fromkeys([*groups, *included])), []
        outbox_id = await self._next_id()
        async with self._unit() as unit:
            session = unit.resource
            if groups is None:
                consumers = self._collection(self._collections.consumers)
                async with unit.operation():
                    rows = await consumers.find(
                        {"destination": {"$in": [envelope.destination, EVERY_DESTINATION]}},
                        {"consumer_group": 1, "_id": 0},
                        session=session,
                    ).to_list()
                owed = list(dict.fromkeys([*sorted(row["consumer_group"] for row in rows), *included]))
            else:
                owed = list(groups)
            async with unit.operation():
                await self._collection(self._collections.events).insert_one(
                    {
                        "_id": outbox_id,
                        "event_id": envelope.event_id,
                        "destination": envelope.destination,
                        "event_type": envelope.event_type,
                        "payload": encode_json(envelope.payload),
                        "headers": encode_json(envelope.headers),
                        "created_at": now,
                    },
                    session=session,
                )
            if owed:
                async with unit.operation():
                    await self._collection(self._collections.deliveries).insert_many(
                        [self._delivery(group, outbox_id, now) for group in owed], session=session
                    )
        return outbox_id

    @staticmethod
    def _delivery(group: str, outbox_id: int, due: datetime) -> dict[str, Any]:
        return {
            "consumer_group": group,
            "outbox_id": outbox_id,
            "available_at": due,
            "attempts": 0,
            "claimed_by": None,
            "done": [],
            "last_error": None,
        }

    # -- consumer groups ------------------------------------------------------------------------------------------

    async def register(
        self,
        group: str,
        destinations: Sequence[str] | None,
        *,
        start: StartPosition | str = StartPosition.LATEST,
    ) -> bool:
        """Register consumer group *group* for *destinations* (``None``: every destination), in a transaction of its
        own; returns whether the group was new.

        The group's destinations become exactly these (every node of a group must register the same ones). A new
        group starts at *start*: with the events published from now on, or (``earliest``) with every event the
        outbox holds for its destinations as well, owed in the same transaction. When several nodes register a new
        group at once, MongoDB aborts all but the first transaction that writes it (a write conflict), and the
        others, run again, find it registered.
        """
        wanted = sorted(set(destinations)) if destinations else [EVERY_DESTINATION]
        position = StartPosition.of(start)
        consumers = self._collection(self._collections.consumers)

        async def work(unit: UnitOfWork) -> bool:
            session = unit.resource
            now = self._clock()
            async with unit.operation():
                rows = await consumers.find(
                    {"consumer_group": group}, {"destination": 1, "_id": 0}, session=session
                ).to_list()
            existing = {row["destination"] for row in rows}
            stale = existing - set(wanted)
            if stale:
                async with unit.operation():
                    await consumers.delete_many(
                        {"consumer_group": group, "destination": {"$in": sorted(stale)}}, session=session
                    )
            inserted = False
            for destination in wanted:
                if destination in existing:
                    continue
                async with unit.operation():
                    result = await consumers.update_one(
                        {"consumer_group": group, "destination": destination},
                        {"$setOnInsert": {"registered_at": now}},
                        upsert=True,
                        session=session,
                    )
                inserted |= result.upserted_id is not None
            new_group = not existing and inserted
            if new_group and position is StartPosition.EARLIEST:
                await self._backfill(unit, group, wanted, now)
            return new_group

        return await self._atomically(work)

    async def _backfill(self, unit: UnitOfWork, group: str, destinations: Sequence[str], now: datetime) -> None:
        """Owe *group* every event the outbox holds for *destinations* that it is not owed yet (a delivery another
        unit wrote meanwhile is left as it is)."""
        session = unit.resource
        events = self._collection(self._collections.events)
        deliveries = self._collection(self._collections.deliveries)
        if EVERY_DESTINATION in destinations:
            # Every destination, but not the events owed only to the groups that were named (event sourcing's).
            query: dict[str, Any] = {"destination": {"$not": {"$regex": f"^{re.escape(ADDRESSED_DESTINATION_PREFIX)}"}}}
        else:
            query = {"destination": {"$in": list(destinations)}}
        fresh = {"available_at": now, "attempts": 0, "claimed_by": None, "done": [], "last_error": None}
        after: int | None = None
        while True:
            page = query if after is None else {**query, "_id": {"$gt": after}}
            async with unit.operation():
                rows = (
                    await events.find(page, {"_id": 1}, session=session).sort("_id", ASCENDING).limit(_BATCH).to_list()
                )
            if not rows:
                return
            async with unit.operation():
                await deliveries.bulk_write(
                    [
                        UpdateOne(
                            {"consumer_group": group, "outbox_id": int(row["_id"])},
                            {"$setOnInsert": fresh},
                            upsert=True,
                        )
                        for row in rows
                    ],
                    ordered=False,
                    session=session,
                )
            if len(rows) < _BATCH:
                return
            after = int(rows[-1]["_id"])

    async def unregister(self, group: str) -> int:
        """Remove consumer group *group*: nothing is owed to it any more (the deliveries it had not made are deleted
        too). Returns how many deliveries were dropped."""

        async def work(unit: UnitOfWork) -> int:
            session = unit.resource
            async with unit.operation():
                await self._collection(self._collections.consumers).delete_many(
                    {"consumer_group": group}, session=session
                )
            async with unit.operation():
                result = await self._collection(self._collections.deliveries).delete_many(
                    {"consumer_group": group}, session=session
                )
            return int(result.deleted_count)

        return await self._atomically(work)

    # -- claiming and settling ------------------------------------------------------------------------------------

    async def claim(self, group: str, *, limit: int, lease: timedelta, owner: str) -> list[Delivery]:
        """Claim up to *limit* deliveries owed to *group* whose time has come, for *lease* (see the module
        documentation); returns them in publication order."""
        now = self._clock()
        until = now + lease
        token = f"{owner}/{uuid.uuid4().hex[:12]}"
        deliveries = self._collection(self._collections.deliveries)
        due: dict[int, datetime] = {}
        async with self._unit(single=True) as unit:
            session = unit.resource
            taken = 0
            for _ in range(_CLAIM_READS):
                async with unit.operation():
                    candidates = (
                        await deliveries.find(
                            {"consumer_group": group, "available_at": {"$lte": now}},
                            {"outbox_id": 1, "available_at": 1, "_id": 0},
                            session=session,
                        )
                        .sort([("available_at", ASCENDING), ("outbox_id", ASCENDING)])
                        .limit(limit - taken)
                        .to_list()
                    )
                if not candidates:
                    break
                read = {int(row["outbox_id"]): row["available_at"] for row in candidates}
                due.update(read)
                async with unit.operation():
                    result = await deliveries.update_many(
                        {"consumer_group": group, "outbox_id": {"$in": list(read)}, "available_at": {"$lte": now}},
                        {"$set": {"available_at": until, "claimed_by": token}, "$inc": {"attempts": 1}},
                        session=session,
                    )
                taken += result.matched_count
                if result.matched_count == len(read) or taken >= limit:
                    break
                # Another relay took some of what was read: they are no longer due, read on past them.
            if not taken:
                return []
            ids = list(due)
            async with unit.operation():
                rows = (
                    await deliveries.find(
                        {"consumer_group": group, "outbox_id": {"$in": ids}, "claimed_by": token}, session=session
                    )
                    .sort("outbox_id", ASCENDING)
                    .to_list()
                )
            if not rows:
                return []
            async with unit.operation():
                stored = (
                    await self._collection(self._collections.events)
                    .find({"_id": {"$in": [int(row["outbox_id"]) for row in rows]}}, session=session)
                    .to_list()
                )
        events = {int(event["_id"]): event for event in stored}
        claimed: list[Delivery] = []
        orphans: list[int] = []
        for row in rows:
            outbox_id = int(row["outbox_id"])
            event = events.get(outbox_id)
            if event is None:
                orphans.append(outbox_id)  # the event was pruned under a backfilled delivery
                continue
            claimed.append(
                Delivery(
                    outbox_id=outbox_id,
                    group=group,
                    envelope=self._envelope(event),
                    attempts=int(row["attempts"]),
                    done=frozenset(row.get("done") or ()),
                    token=token,
                    last_error=row.get("last_error"),
                    leased_until=until,
                    due_at=due.get(outbox_id),
                )
            )
        if orphans:
            async with self._unit(single=True) as unit, unit.operation():
                await deliveries.delete_many(
                    {"consumer_group": group, "outbox_id": {"$in": orphans}, "claimed_by": token},
                    session=unit.resource,
                )
        return claimed

    @staticmethod
    def _envelope(event: dict[str, Any]) -> EventEnvelope:
        headers = event.get("headers")
        return EventEnvelope(
            event_type=event["event_type"],
            payload=json.loads(event["payload"]),
            destination=event["destination"],
            event_id=event["event_id"],
            timestamp=event["created_at"],
            headers=json.loads(headers) if headers else {},
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
        """Record what became of a claimed delivery: the subscriptions that handled it (*done*), the ones that go to
        the dead letters (*dead*: subscription key and failure), and then either the next attempt at *retry_at* (of
        the others) or the end of the delivery. One command, or with dead letters one transaction. Returns
        ``False``, and records nothing, when another relay claimed the delivery since (its lease had ended)."""
        deliveries = self._collection(self._collections.deliveries)
        mine = {"consumer_group": delivery.group, "outbox_id": delivery.outbox_id, "claimed_by": delivery.token}
        handled = sorted(set(done))

        async def write(unit: UnitOfWork) -> bool:
            """The settling command, fenced by the claim; whether the delivery was still this claim's."""
            session = unit.resource
            async with unit.operation():
                if retry_at is None:
                    return bool((await deliveries.delete_one(mine, session=session)).deleted_count)
                result = await deliveries.update_one(
                    mine,
                    {
                        "$set": {
                            "available_at": retry_at,
                            "claimed_by": None,
                            "done": handled,
                            "last_error": _truncated(error) if error else None,
                        }
                    },
                    session=session,
                )
                return bool(result.matched_count)

        async def with_dead_letters(unit: UnitOfWork) -> None:
            failed_at = self._clock()
            async with unit.operation():
                await self._collection(self._collections.dead_letters).insert_many(
                    [self._dead_letter(delivery, key, failure, failed_at) for key, failure in dead],
                    session=unit.resource,
                )
            if not await write(unit):
                raise _LeaseLost  # the dead letters roll back with the transaction

        settled = True
        try:
            if dead:
                await self._atomically(with_dead_letters)
            else:
                async with self._unit(single=True) as unit:
                    settled = await write(unit)
        except _LeaseLost:
            settled = False
        if not settled:
            _logger.warning(
                "outbox_delivery_claimed_again",
                extra={
                    "group": delivery.group,
                    "outbox_id": delivery.outbox_id,
                    "event_id": delivery.envelope.event_id,
                },
            )
        return settled

    @staticmethod
    def _dead_letter(
        delivery: Delivery, subscription: str, failure: BaseException, failed_at: datetime
    ) -> dict[str, Any]:
        envelope = delivery.envelope
        return {
            "_id": str(uuid.uuid4()),
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

    async def complete(self, deliveries: Sequence[Delivery]) -> int:
        """Settle claimed deliveries every subscription handled: their documents go, in one command per claim.
        Returns how many were settled; the others had been claimed again by another relay since (their lease had
        ended), which handles them again."""
        collection = self._collection(self._collections.deliveries)
        settled = 0
        for (group, token), claimed in _by_claim(deliveries).items():
            ids = [delivery.outbox_id for delivery in claimed]
            async with self._unit(single=True) as unit, unit.operation():
                result = await collection.delete_many(
                    {"consumer_group": group, "outbox_id": {"$in": ids}, "claimed_by": token}, session=unit.resource
                )
            count = int(result.deleted_count)
            settled += count
            if count < len(ids):
                _logger.warning(
                    "outbox_deliveries_claimed_again",
                    extra={"group": group, "deliveries": len(ids) - count, "claim": token},
                )
        return settled

    async def extend(self, deliveries: Sequence[Delivery], *, until: datetime) -> list[Delivery]:
        """Extend the lease of claimed deliveries to *until*, in one command per claim (fenced by the claim: a
        delivery another relay claimed since is left alone). Returns the deliveries the relay still holds, in their
        order, with their new lease."""
        collection = self._collection(self._collections.deliveries)
        held: set[tuple[str, str, int]] = set()
        for (group, token), claimed in _by_claim(deliveries).items():
            ids = [delivery.outbox_id for delivery in claimed]
            mine = {"consumer_group": group, "outbox_id": {"$in": ids}, "claimed_by": token}
            async with self._unit(single=True) as unit:
                session = unit.resource
                async with unit.operation():
                    result = await collection.update_many(mine, {"$set": {"available_at": until}}, session=session)
                kept = ids
                if result.matched_count < len(ids):
                    async with unit.operation():
                        rows = await collection.find(mine, {"outbox_id": 1, "_id": 0}, session=session).to_list()
                    kept = [int(row["outbox_id"]) for row in rows]
            held.update((group, token, outbox_id) for outbox_id in kept)
        return [
            replace(delivery, leased_until=until)
            for delivery in deliveries
            if (delivery.group, delivery.token, delivery.outbox_id) in held
        ]

    async def release(self, deliveries: Sequence[Delivery]) -> int:
        """Give claimed deliveries back unattempted (a relay that stops, or a round whose lease ran short): each is
        due again when it was before the claim, so it keeps its place, and the claim does not count as an attempt.
        One command per claim. Returns how many were given back."""
        if not deliveries:
            return 0
        collection = self._collection(self._collections.deliveries)
        now = self._clock()
        released = 0
        for (group, token), claimed in _by_claim(deliveries).items():
            async with self._unit(single=True) as unit, unit.operation():
                result = await collection.bulk_write(
                    [
                        UpdateOne(
                            {"consumer_group": group, "outbox_id": delivery.outbox_id, "claimed_by": token},
                            {
                                "$set": {"available_at": delivery.due_at or now, "claimed_by": None},
                                "$inc": {"attempts": -1},
                            },
                        )
                        for delivery in claimed
                    ],
                    ordered=False,
                    session=unit.resource,
                )
            released += int(result.matched_count)
        return released

    # -- reading --------------------------------------------------------------------------------------------------

    async def pending(self, group: str, *, limit: int = 1000) -> list[PendingDelivery]:
        """The deliveries still owed to *group* (claimed ones included), oldest first."""
        async with self._unit(read_only=True) as unit, unit.operation():
            rows = await (
                await self._collection(self._collections.deliveries).aggregate(
                    [
                        {"$match": {"consumer_group": group}},
                        {"$sort": {"outbox_id": 1}},
                        {
                            "$lookup": {
                                "from": self._collections.events,
                                "localField": "outbox_id",
                                "foreignField": "_id",
                                "as": "event",
                            }
                        },
                        {"$unwind": "$event"},
                        {"$limit": limit},
                    ],
                    session=unit.resource,
                )
            ).to_list()
        return [
            PendingDelivery(
                outbox_id=int(row["outbox_id"]),
                envelope=self._envelope(row["event"]),
                attempts=int(row["attempts"]),
                available_at=row["available_at"],
                last_error=row.get("last_error"),
            )
            for row in rows
        ]

    async def dead_letters(self, group: str | None = None, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        """The dead letters (of *group*, or of every group), most recent first."""
        query: dict[str, Any] = {} if group is None else {"consumer_group": group}
        async with self._unit(read_only=True) as unit, unit.operation():
            rows = (
                await self._collection(self._collections.dead_letters)
                .find(query, session=unit.resource)
                .sort([("failed_at", DESCENDING), ("_id", ASCENDING)])
                .limit(limit)
                .to_list()
            )
        return [
            EdaDeadLetterEntry(
                id=row["_id"],
                event=EventEnvelope(
                    event_type=row["event_type"],
                    payload=json.loads(row["payload"]),
                    destination=row["destination"],
                    event_id=row["event_id"],
                    timestamp=row["occurred_at"],
                    headers=json.loads(row["headers"]) if row.get("headers") else {},
                ),
                error_type=row["error_type"],
                error_message=row["error_message"],
                timestamp=row["failed_at"],
                attempts=int(row["attempts"]),
                group=row.get("consumer_group"),
                subscription=row.get("subscription"),
            )
            for row in rows
        ]

    # -- retention ------------------------------------------------------------------------------------------------

    async def prune(self, retention: Retention) -> PruneResult:
        """Delete what *retention* lets go, in batches, each a transaction of its own (see
        :class:`~pyfly.eda.ports.outbox.Retention`)."""
        events = self._collection(self._collections.events)
        deliveries = self._collection(self._collections.deliveries)
        now = self._clock()
        delivered = expired = undelivered = 0
        if retention.delivered is not None:
            cutoff = now - retention.delivered

            async def handled(unit: UnitOfWork) -> int:
                # Deliveries are written in the transaction that writes their event, so an event that is visible
                # with no delivery left has been handled by every group it was owed to.
                async with unit.operation():
                    rows = await (
                        await events.aggregate(
                            [
                                {"$match": {"created_at": {"$lt": cutoff}}},
                                {"$sort": {"_id": 1}},
                                {
                                    "$lookup": {
                                        "from": self._collections.deliveries,
                                        "localField": "_id",
                                        "foreignField": "outbox_id",
                                        "as": "owed",
                                    }
                                },
                                {"$match": {"owed": {"$size": 0}}},
                                {"$limit": retention.batch_size},
                                {"$project": {"_id": 1}},
                            ],
                            session=unit.resource,
                        )
                    ).to_list()
                ids = [int(row["_id"]) for row in rows]
                if ids:
                    async with unit.operation():
                        await events.delete_many({"_id": {"$in": ids}}, session=unit.resource)
                return len(ids)

            while True:
                count = await self._atomically(handled)
                delivered += count
                if count < retention.batch_size:
                    break
        if retention.max_age is not None:
            cutoff = now - retention.max_age

            async def aged(unit: UnitOfWork) -> tuple[int, int]:
                session = unit.resource
                async with unit.operation():
                    rows = (
                        await events.find({"created_at": {"$lt": cutoff}}, {"_id": 1}, session=session)
                        .sort("_id", ASCENDING)
                        .limit(retention.batch_size)
                        .to_list()
                    )
                ids = [int(row["_id"]) for row in rows]
                if not ids:
                    return 0, 0
                async with unit.operation():
                    dropped = await deliveries.delete_many({"outbox_id": {"$in": ids}}, session=session)
                async with unit.operation():
                    await events.delete_many({"_id": {"$in": ids}}, session=session)
                return len(ids), int(dropped.deleted_count)

            while True:
                count, dropped = await self._atomically(aged)
                expired += count
                undelivered += dropped
                if count < retention.batch_size:
                    break
            if undelivered:
                _logger.warning(
                    "outbox_undelivered_events_expired",
                    extra={"events": expired, "deliveries": undelivered, "max_age": str(retention.max_age)},
                )
        return PruneResult(delivered=delivered, expired=expired, undelivered=undelivered)
