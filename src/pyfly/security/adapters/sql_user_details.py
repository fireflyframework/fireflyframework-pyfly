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
"""SQL table-backed :class:`UserDetailsService` (Spring's ``JdbcUserDetailsManager``).

Durable user/credential storage for HTTP Basic / form login, backed by any
SQLAlchemy ``AsyncEngine``. Hexagonal: the engine is injected lazily by the
composition root; no SQLAlchemy import at module scope. The table is created
lazily and idempotently on first use.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any

from pyfly.security.user_details import UserDetails

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlUserDetailsService:
    """A :class:`UserDetailsService` storing users in a SQL table.

    Columns: ``username`` (PK), ``password_hash``, ``roles`` (JSON), ``permissions``
    (JSON), ``enabled`` (int). Works on PostgreSQL and SQLite (``ON CONFLICT`` upsert).
    """

    def __init__(self, engine_factory: Callable[[], Any], *, table: str = "pyfly_users") -> None:
        if not _IDENT.match(table):
            raise ValueError(f"Invalid user-store table name: {table!r}")
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
                        "username TEXT PRIMARY KEY, "
                        "password_hash TEXT NOT NULL, "
                        "roles TEXT NOT NULL DEFAULT '[]', "
                        "permissions TEXT NOT NULL DEFAULT '[]', "
                        "enabled INTEGER NOT NULL DEFAULT 1)"
                    )
                )
            self._ensured = True

    async def load_user_by_username(self, username: str) -> UserDetails | None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT username, password_hash, roles, permissions, enabled "
                    f"FROM {self._table} WHERE username = :u"
                ),
                {"u": username},
            )
            row = result.first()
        if row is None:
            return None
        return UserDetails(
            username=row[0],
            password_hash=row[1],
            roles=list(json.loads(row[2] or "[]")),
            permissions=list(json.loads(row[3] or "[]")),
            enabled=bool(row[4]),
        )

    async def save(self, user: UserDetails) -> None:
        """Insert or update *user* (keyed by username)."""
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {self._table} (username, password_hash, roles, permissions, enabled) "
                    "VALUES (:u, :p, :r, :perm, :e) "
                    "ON CONFLICT (username) DO UPDATE SET "
                    "password_hash = excluded.password_hash, roles = excluded.roles, "
                    "permissions = excluded.permissions, enabled = excluded.enabled"
                ),
                {
                    "u": user.username,
                    "p": user.password_hash,
                    "r": json.dumps(list(user.roles)),
                    "perm": json.dumps(list(user.permissions)),
                    "e": 1 if user.enabled else 0,
                },
            )

    async def delete(self, username: str) -> None:
        from sqlalchemy import text

        await self._ensure_table()
        async with self._eng().begin() as conn:
            await conn.execute(text(f"DELETE FROM {self._table} WHERE username = :u"), {"u": username})
