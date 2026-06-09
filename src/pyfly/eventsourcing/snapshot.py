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
"""Snapshot store SPI + in-memory + SQL adapters."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


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
    """Async SQL adapter for the snapshot store.

    Expects an ``AsyncEngine``; uses raw SQL so it works on any backend.
    Caller must run :meth:`initialize` once.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS pyfly_snapshots (
        aggregate_id   VARCHAR(64) PRIMARY KEY,
        aggregate_type VARCHAR(255) NOT NULL,
        sequence       INTEGER NOT NULL,
        payload        TEXT NOT NULL,
        created_at     TIMESTAMP NOT NULL
    )
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def initialize(self) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.begin() as conn:
            await conn.execute(text(self.DDL))

    async def save(self, snapshot: Snapshot) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        payload_json = json.dumps(snapshot.payload)
        created_at = datetime.now(UTC).replace(tzinfo=None)
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO pyfly_snapshots
                        (aggregate_id, aggregate_type, sequence, payload, created_at)
                    VALUES (:aid, :atype, :seq, :payload, :created_at)
                    ON CONFLICT (aggregate_id) DO UPDATE SET
                        aggregate_type = EXCLUDED.aggregate_type,
                        sequence       = EXCLUDED.sequence,
                        payload        = EXCLUDED.payload,
                        created_at     = EXCLUDED.created_at
                    WHERE pyfly_snapshots.sequence < EXCLUDED.sequence
                    """
                ),
                {
                    "aid": snapshot.aggregate_id,
                    "atype": snapshot.aggregate_type,
                    "seq": snapshot.sequence,
                    "payload": payload_json,
                    "created_at": created_at,
                },
            )

    async def load(self, aggregate_id: str) -> Snapshot | None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """SELECT aggregate_id, aggregate_type, sequence, payload
                           FROM pyfly_snapshots WHERE aggregate_id = :aid"""
                    ),
                    {"aid": aggregate_id},
                )
            ).first()
        if row is None:
            return None
        return Snapshot(
            aggregate_id=row[0],
            aggregate_type=row[1],
            sequence=row[2],
            payload=json.loads(row[3]),
        )

    async def delete(self, aggregate_id: str) -> bool:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM pyfly_snapshots WHERE aggregate_id = :aid"),
                {"aid": aggregate_id},
            )
        return bool(result.rowcount > 0)
