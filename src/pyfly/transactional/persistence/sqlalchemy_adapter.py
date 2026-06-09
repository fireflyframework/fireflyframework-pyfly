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
"""SQLAlchemy / async-SQL persistence adapter for orchestration state.

Mirrors the Java engine's R2DBC-based persistence.  The adapter expects an
``AsyncEngine`` (or callable ``async with`` factory) and a single table:

    CREATE TABLE pyfly_orchestration_state (
        correlation_id  VARCHAR(64) PRIMARY KEY,
        execution_name  VARCHAR(255) NOT NULL,
        pattern         VARCHAR(32) NOT NULL,
        status          VARCHAR(32) NOT NULL,
        started_at      TIMESTAMPTZ NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL,
        completed_at    TIMESTAMPTZ NULL,
        payload         TEXT NOT NULL
    );

Designed to fail gracefully (no module-level import of SQLAlchemy) so the
engine still starts even if the optional ``data-relational`` extra is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.transactional.core.persistence import (
    ExecutionState,
    StateSerializer,
)


class SqlAlchemyPersistenceProvider:
    """Async SQLAlchemy adapter — uses raw SQL for portability across PG/MySQL."""

    DDL = """
    CREATE TABLE IF NOT EXISTS pyfly_orchestration_state (
        correlation_id VARCHAR(64) PRIMARY KEY,
        execution_name VARCHAR(255) NOT NULL,
        pattern VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL,
        started_at TIMESTAMP NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        completed_at TIMESTAMP NULL,
        payload TEXT NOT NULL
    )
    """

    def __init__(self, engine: Any, *, table_name: str = "pyfly_orchestration_state") -> None:
        self._engine = engine
        self._table = table_name

    async def initialize(self) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        async with self._engine.begin() as conn:
            await conn.execute(text(self.DDL))

    async def save(self, state: ExecutionState) -> None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        raw = StateSerializer.serialize(state)
        sql = text(
            f"""
            INSERT INTO {self._table}
                (correlation_id, execution_name, pattern, status, started_at, updated_at, completed_at, payload)
            VALUES (:cid, :name, :pattern, :status, :started, :updated, :completed, :payload)
            ON CONFLICT (correlation_id) DO UPDATE SET
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at,
                completed_at = EXCLUDED.completed_at,
                payload = EXCLUDED.payload
            """
        )

        # Strip tzinfo for TIMESTAMP columns — asyncpg rejects tz-aware datetimes
        # against TIMESTAMP WITHOUT TIME ZONE.  Full tz info is preserved in the
        # JSON payload and restored on deserialisation.
        def _naive(dt: datetime | None) -> datetime | None:
            return dt.replace(tzinfo=None) if dt is not None else None

        async with self._engine.begin() as conn:
            await conn.execute(
                sql,
                {
                    "cid": state.correlation_id,
                    "name": state.name,
                    "pattern": state.pattern.value,
                    "status": state.status.value,
                    "started": _naive(state.started_at),
                    "updated": _naive(state.updated_at),
                    "completed": _naive(state.completed_at),
                    "payload": raw,
                },
            )

    async def find(self, correlation_id: str) -> ExecutionState | None:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        sql = text(f"SELECT payload FROM {self._table} WHERE correlation_id = :cid")
        async with self._engine.connect() as conn:
            row = (await conn.execute(sql, {"cid": correlation_id})).first()
        if row is None:
            return None
        return StateSerializer.deserialize(row[0])

    async def find_all(self, *, status: Any = None, pattern: Any = None) -> list[ExecutionState]:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        clauses: list[str] = []
        params: dict[str, Any] = {}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if pattern is not None:
            clauses.append("pattern = :pattern")
            params["pattern"] = pattern.value
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = text(f"SELECT payload FROM {self._table} {where}")
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, params)).fetchall()
        return [StateSerializer.deserialize(r[0]) for r in rows]

    async def find_stale(self, before: datetime) -> list[ExecutionState]:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        sql = text(
            f"""SELECT payload FROM {self._table}
                WHERE updated_at < :before
                AND status NOT IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT','CONFIRMED','CANCELED','COMPENSATED')"""
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(sql, {"before": before})).fetchall()
        return [StateSerializer.deserialize(r[0]) for r in rows]

    async def delete(self, correlation_id: str) -> bool:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        sql = text(f"DELETE FROM {self._table} WHERE correlation_id = :cid")
        async with self._engine.begin() as conn:
            result = await conn.execute(sql, {"cid": correlation_id})
        return bool(result.rowcount > 0)

    async def cleanup(self, older_than: timedelta) -> int:
        from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

        cutoff = datetime.now(UTC) - older_than
        sql = text(
            f"""DELETE FROM {self._table}
                WHERE status IN ('COMPLETED','FAILED','CANCELLED','TIMED_OUT','CONFIRMED','CANCELED','COMPENSATED')
                  AND COALESCE(completed_at, updated_at) < :cutoff"""
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(sql, {"cutoff": cutoff})
        return int(result.rowcount)

    async def is_healthy(self) -> bool:
        try:
            from sqlalchemy import text  # type: ignore[import-not-found, unused-ignore]

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False
