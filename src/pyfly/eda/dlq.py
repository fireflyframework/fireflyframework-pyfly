# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Dead-letter queue for the EDA module — capture events that fail processing.

An :class:`EdaDeadLetterStore` records the events whose handlers failed on every attempt:

- the outbox buses (``pyfly.eda.provider`` ``database`` or ``postgres``) write their dead letters to the
  outbox's own table, ``pyfly_outbox_dead_letters``, in the unit that settles the delivery; given a store,
  they hand them to it instead;
- the Kafka and RabbitMQ buses dead-letter to their broker (``<topic>.DLT``, the dead-letter exchange) and
  record the event in the store the application defines as a bean, if any: after the broker has it, and best
  effort there (a failure is logged and counted on ``dead_letter_store_failures``, not retried), except that
  with the Kafka dead-letter topic off the store is the only copy and the record waits for it.

:class:`SqlEdaDeadLetterStore` is the durable store, on the outbox's dead-letter table of any datasource;
:class:`InMemoryEdaDeadLetterStore` keeps them in the process, for tests.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope

if TYPE_CHECKING:
    from sqlalchemy import Table


@dataclass
class EdaDeadLetterEntry:
    """One dead letter: the event, the last failure, and how many attempts were made. *group* and
    *subscription* say which consumer group and which of its subscriptions failed, when the bus knows."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event: EventEnvelope = field(default_factory=lambda: EventEnvelope("", {}, ""))
    error_type: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 0
    group: str | None = None
    subscription: str | None = None


@runtime_checkable
class EdaDeadLetterStore(Protocol):
    async def add(self, entry: EdaDeadLetterEntry) -> None: ...
    async def list(self, *, limit: int = 100) -> list[EdaDeadLetterEntry]: ...
    async def delete(self, entry_id: str) -> bool: ...


class InMemoryEdaDeadLetterStore:
    def __init__(self) -> None:
        self._store: dict[str, EdaDeadLetterEntry] = {}
        self._lock = asyncio.Lock()

    async def add(self, entry: EdaDeadLetterEntry) -> None:
        async with self._lock:
            self._store[entry.id] = entry

    async def list(self, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        async with self._lock:
            entries = list(self._store.values())
        return sorted(entries, key=lambda e: e.timestamp, reverse=True)[:limit]

    async def delete(self, entry_id: str) -> bool:
        async with self._lock:
            return self._store.pop(entry_id, None) is not None


class SqlEdaDeadLetterStore:
    """A durable :class:`EdaDeadLetterStore` on the outbox's dead-letter table (``pyfly_outbox_dead_letters``).

    *datasource* is where the table lives: a datasource name, a registry ``DataSource``, an ``AsyncEngine`` or
    ``None`` (the default datasource). *table* is the table (a ``Table`` or its name). An entry is written in
    the unit of work bound for that datasource, or in a short unit of its own. With *create_table* false,
    :meth:`start` only checks the table. Define it as a bean to have the Kafka and RabbitMQ buses record their
    dead letters durably::

        @bean
        def dead_letters(self) -> EdaDeadLetterStore:
            return SqlEdaDeadLetterStore("primary")
    """

    def __init__(
        self, datasource: object = None, *, table: Table | str | None = None, create_table: bool = True
    ) -> None:
        self._target = datasource
        self._table_spec = table
        self._table_object: Table | None = None
        self._create_table = create_table

    @property
    def table(self) -> Table:
        """The dead-letter table."""
        if self._table_object is None:
            from sqlalchemy import Table

            from pyfly.data.relational.framework_schema import OUTBOX_DEAD_LETTERS, outbox_dead_letters_table

            spec = self._table_spec
            if isinstance(spec, Table):
                self._table_object = spec
            else:
                self._table_object = outbox_dead_letters_table(spec or OUTBOX_DEAD_LETTERS)
        return self._table_object

    async def start(self) -> None:
        """Create the table when allowed, and check it."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, self.table, create=self._create_table)

    async def stop(self) -> None:
        """Nothing to release: the store borrows the datasource's connections."""

    async def add(self, entry: EdaDeadLetterEntry) -> None:
        """Record *entry*."""
        from sqlalchemy import insert

        from pyfly.data.transaction import infrastructure_unit
        from pyfly.eda.outbox import encode_json

        event = entry.event
        values: dict[str, Any] = {
            "id": entry.id,
            "consumer_group": entry.group,
            "subscription": entry.subscription,
            "event_id": event.event_id,
            "destination": event.destination,
            "event_type": event.event_type,
            "payload": encode_json(event.payload),
            "headers": encode_json(event.headers),
            "occurred_at": event.timestamp,
            "error_type": entry.error_type[:255],
            "error_message": entry.error_message,
            "attempts": entry.attempts,
            "failed_at": entry.timestamp,
        }
        async with infrastructure_unit(self._target, single_statement=True) as session:
            await session.execute(insert(self.table).values(values))

    async def list(self, *, limit: int = 100, group: str | None = None) -> list[EdaDeadLetterEntry]:
        """The most recent entries first (of consumer group *group* when given)."""
        from sqlalchemy import select

        from pyfly.data.transaction import infrastructure_unit

        table = self.table
        query = select(table).order_by(table.c.failed_at.desc(), table.c.id).limit(limit)
        if group is not None:
            query = query.where(table.c.consumer_group == group)
        async with infrastructure_unit(self._target, read_only=True) as session:
            rows = (await session.execute(query)).mappings().all()
        return [self._entry(row) for row in rows]

    async def delete(self, entry_id: str) -> bool:
        """Delete the entry *entry_id* (once it was dealt with); returns whether it existed."""
        from sqlalchemy import delete

        from pyfly.data.transaction import infrastructure_unit

        table = self.table
        async with infrastructure_unit(self._target, single_statement=True) as session:
            result = await session.execute(delete(table).where(table.c.id == entry_id))
        return int(getattr(result, "rowcount", 0) or 0) > 0

    @staticmethod
    def _entry(row: Any) -> EdaDeadLetterEntry:
        return EdaDeadLetterEntry(
            id=row["id"],
            event=EventEnvelope(
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                destination=row["destination"],
                event_id=row["event_id"],
                timestamp=row["occurred_at"],
                headers=json.loads(row["headers"]) if row["headers"] else {},
            ),
            error_type=row["error_type"],
            error_message=row["error_message"],
            timestamp=row["failed_at"],
            attempts=int(row["attempts"]),
            group=row["consumer_group"],
            subscription=row["subscription"],
        )
