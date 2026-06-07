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
"""Postgres table-backed OAuth2 :class:`TokenStore` adapter.

Durable, auditable refresh-token storage + cross-instance revocation for a multi-instance
authorization server, with no Redis required. Hexagonal: the SQLAlchemy ``AsyncEngine`` is
injected lazily by the composition root; this module imports no SQLAlchemy at module scope.
The backing table is created lazily and idempotently on first use.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresTokenStore:
    """OAuth2 token store in a Postgres table (token_id PK, data JSON text)."""

    def __init__(self, engine_factory: Callable[[], Any], *, table: str = "pyfly_oauth2_tokens") -> None:
        if not _IDENT.match(table):
            raise ValueError(f"Invalid token-store table name: {table!r}")
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
                    text(f"CREATE TABLE IF NOT EXISTS {self._table} (token_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                )
            self._ensured = True

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {self._table} (token_id, data) VALUES (:i, :d) "
                    "ON CONFLICT (token_id) DO UPDATE SET data = EXCLUDED.data"
                ),
                {"i": token_id, "d": json.dumps(token_data)},
            )

    async def find(self, token_id: str) -> dict[str, Any] | None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().connect() as conn:
            result = await conn.execute(
                text(f"SELECT data FROM {self._table} WHERE token_id = :i"),
                {"i": token_id},
            )
            row = result.first()
            if row is None:
                return None
            data: dict[str, Any] = json.loads(row[0])
            return data

    async def revoke(self, token_id: str) -> None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(text(f"DELETE FROM {self._table} WHERE token_id = :i"), {"i": token_id})
