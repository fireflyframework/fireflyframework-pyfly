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
"""EventStore SPI plus in-memory + SQL adapters."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, runtime_checkable

from pyfly.eventsourcing.event import StoredEventEnvelope


class ConcurrencyError(Exception):
    """Optimistic-locking failure: expected version did not match the store's."""


@runtime_checkable
class EventStore(Protocol):
    """Append, load and stream events for aggregates."""

    async def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        events: list[StoredEventEnvelope],
        *,
        expected_version: int,
    ) -> None: ...

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]: ...

    async def stream_all(self, *, after_event_id: str | None = None, limit: int = 100) -> list[StoredEventEnvelope]: ...

    async def latest_version(self, aggregate_id: str) -> int: ...


class InMemoryEventStore:
    """Default zero-dep adapter: list per aggregate, global event log."""

    def __init__(self) -> None:
        self._by_aggregate: dict[str, list[StoredEventEnvelope]] = {}
        self._all: list[StoredEventEnvelope] = []
        self._lock = asyncio.Lock()

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
                raise ConcurrencyError(msg)
            for evt in events:
                evt.aggregate_id = aggregate_id
                evt.aggregate_type = aggregate_type
                evt.sequence = len(current) + 1
                current.append(evt)
                self._all.append(evt)
            self._by_aggregate[aggregate_id] = current

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]:
        async with self._lock:
            events = self._by_aggregate.get(aggregate_id, [])
            return [e for e in events if e.sequence > after_sequence]

    async def stream_all(self, *, after_event_id: str | None = None, limit: int = 100) -> list[StoredEventEnvelope]:
        async with self._lock:
            if after_event_id is None:
                return list(self._all[:limit])
            for idx, evt in enumerate(self._all):
                if evt.event_id == after_event_id:
                    return list(self._all[idx + 1 : idx + 1 + limit])
            return []

    async def latest_version(self, aggregate_id: str) -> int:
        async with self._lock:
            return len(self._by_aggregate.get(aggregate_id, []))


class SqlAlchemyEventStore:
    """Async SQL adapter for the event store.

    Expects an ``AsyncEngine``; uses raw SQL so it works on any backend.
    Caller must run :meth:`initialize` once.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS pyfly_event_store (
        event_id        VARCHAR(64) PRIMARY KEY,
        aggregate_id    VARCHAR(64) NOT NULL,
        aggregate_type  VARCHAR(255) NOT NULL,
        sequence        INTEGER NOT NULL,
        event_type      VARCHAR(255) NOT NULL,
        payload         TEXT NOT NULL,
        metadata        TEXT NOT NULL,
        occurred_at     TIMESTAMP NOT NULL,
        version         INTEGER NOT NULL,
        tenant_id       VARCHAR(64) NULL,
        UNIQUE (aggregate_id, sequence)
    )
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def initialize(self) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.begin() as conn:
            await conn.execute(text(self.DDL))

    async def append(
        self,
        aggregate_id: str,
        aggregate_type: str,
        events: list[StoredEventEnvelope],
        *,
        expected_version: int,
    ) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        latest = await self.latest_version(aggregate_id)
        if latest != expected_version:
            msg = f"expected version {expected_version}, found {latest}"
            raise ConcurrencyError(msg)
        async with self._engine.begin() as conn:
            for i, evt in enumerate(events, start=1):
                evt.aggregate_id = aggregate_id
                evt.aggregate_type = aggregate_type
                evt.sequence = expected_version + i
                await conn.execute(
                    text(
                        """
                        INSERT INTO pyfly_event_store
                            (event_id, aggregate_id, aggregate_type, sequence,
                             event_type, payload, metadata, occurred_at, version, tenant_id)
                        VALUES (:eid, :aid, :atype, :seq, :etype, :payload, :meta, :occurred, :ver, :tenant)
                        """
                    ),
                    {
                        "eid": evt.event_id,
                        "aid": evt.aggregate_id,
                        "atype": evt.aggregate_type,
                        "seq": evt.sequence,
                        "etype": evt.event_type,
                        "payload": evt.to_json(),
                        "meta": "{}",
                        "occurred": evt.occurred_at,
                        "ver": evt.version,
                        "tenant": evt.tenant_id,
                    },
                )

    async def load(self, aggregate_id: str, *, after_sequence: int = 0) -> list[StoredEventEnvelope]:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """SELECT payload FROM pyfly_event_store
                           WHERE aggregate_id = :aid AND sequence > :after
                           ORDER BY sequence"""
                    ),
                    {"aid": aggregate_id, "after": after_sequence},
                )
            ).fetchall()
        return [StoredEventEnvelope.from_json(r[0]) for r in rows]

    async def stream_all(self, *, after_event_id: str | None = None, limit: int = 100) -> list[StoredEventEnvelope]:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.connect() as conn:
            if after_event_id is None:
                rows = (
                    await conn.execute(
                        text("SELECT payload FROM pyfly_event_store ORDER BY occurred_at LIMIT :limit"),
                        {"limit": limit},
                    )
                ).fetchall()
            else:
                rows = (
                    await conn.execute(
                        text(
                            """SELECT payload FROM pyfly_event_store
                               WHERE occurred_at >= (
                                   SELECT occurred_at FROM pyfly_event_store WHERE event_id = :eid)
                               AND event_id != :eid
                               ORDER BY occurred_at LIMIT :limit"""
                        ),
                        {"eid": after_event_id, "limit": limit},
                    )
                ).fetchall()
        return [StoredEventEnvelope.from_json(r[0]) for r in rows]

    async def latest_version(self, aggregate_id: str) -> int:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT COALESCE(MAX(sequence), 0) FROM pyfly_event_store WHERE aggregate_id = :aid"),
                {"aid": aggregate_id},
            )
            return int(result.scalar() or 0)
