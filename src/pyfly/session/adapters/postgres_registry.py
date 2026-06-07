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
"""Postgres table-backed :class:`~pyfly.session.concurrency.SessionRegistry` adapter.

Durable, queryable, cross-process session concurrency control for relational-only deployments
(no Redis required) — the user's "postgres, not just redis". Hexagonal: the SQLAlchemy
``AsyncEngine`` is injected (lazily, via a factory) by the composition root; this module imports
no SQLAlchemy at module scope. The backing table is created lazily and idempotently on first use.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

# Guard against SQL injection via a misconfigured table name (it is interpolated, not bound).
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresSessionRegistry:
    """Per-principal session index in a Postgres table (session_id PK, principal, created_at)."""

    def __init__(self, engine_factory: Callable[[], Any], *, table: str = "pyfly_session_registry") -> None:
        if not _IDENT.match(table):
            raise ValueError(f"Invalid session-registry table name: {table!r}")
        self._engine_factory = engine_factory
        self._engine: Any = None
        self._table = table
        self._ensured = False
        self._guard = asyncio.Lock()

    def _eng(self) -> Any:
        if self._engine is None:
            self._engine = self._engine_factory()
        return self._engine

    async def _ensure_table(self) -> None:
        if self._ensured:
            return
        from sqlalchemy import text

        async with self._guard:
            if self._ensured:
                return
            async with self._eng().begin() as conn:
                await conn.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {self._table} ("
                        "session_id TEXT PRIMARY KEY, principal TEXT NOT NULL, "
                        "created_at DOUBLE PRECISION NOT NULL)"
                    )
                )
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {self._table}_principal_idx ON {self._table} (principal)")
                )
            self._ensured = True

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {self._table} (session_id, principal, created_at) "
                    "VALUES (:s, :p, :c) ON CONFLICT (session_id) "
                    "DO UPDATE SET principal = EXCLUDED.principal, created_at = EXCLUDED.created_at"
                ),
                {"s": session_id, "p": principal, "c": created_at},
            )

    async def deregister(self, principal: str, session_id: str) -> None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {self._table} WHERE principal = :p AND session_id = :s"),
                {"p": principal, "s": session_id},
            )

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().connect() as conn:
            result = await conn.execute(
                text(f"SELECT session_id, created_at FROM {self._table} WHERE principal = :p ORDER BY created_at ASC"),
                {"p": principal},
            )
            return [(row[0], float(row[1])) for row in result.fetchall()]  # ORDER BY -> oldest first

    async def count(self, principal: str) -> int:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().connect() as conn:
            result = await conn.execute(
                text(f"SELECT COUNT(*) FROM {self._table} WHERE principal = :p"),
                {"p": principal},
            )
            return int(result.scalar() or 0)
