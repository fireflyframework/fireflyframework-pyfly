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

Durable user/credential storage for HTTP Basic / form login, on any relational backend SQLAlchemy
supports. The users live in the framework table ``pyfly_users``
(:func:`~pyfly.data.relational.framework_schema.users_table`), created at :meth:`SqlUserDetailsService.start`
or on first use (idempotently), and ``save`` is the dialect's upsert (:mod:`pyfly.data.relational.upsert`).
Every operation runs through :func:`~pyfly.data.transaction.infrastructure_unit`: a login lookup outside a
transaction is one autocommit statement on PostgreSQL, and a ``save`` inside a unit of work on the store's
datasource is part of it. Hexagonal: the engine is injected lazily by the composition root; no SQLAlchemy
import at module scope.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import infrastructure_unit
from pyfly.security.user_details import UserDetails

if TYPE_CHECKING:
    from sqlalchemy import Table

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlUserDetailsService:
    """A :class:`UserDetailsService` storing users in a SQL table.

    Columns: ``username`` (PK), ``password_hash``, ``roles`` (JSON), ``permissions`` (JSON), ``enabled``
    (int). *engine_factory* is the store's datasource (an ``AsyncEngine``, a registry ``DataSource`` or a
    datasource name) or a zero-argument callable returning it, resolved once, at first use. With
    *create_table* false the table is only checked, never created.
    """

    def __init__(
        self,
        engine_factory: Callable[[], Any] | Any,
        *,
        table: str = "pyfly_users",
        create_table: bool = True,
    ) -> None:
        if not _IDENT.match(table):
            raise ValueError(f"Invalid user-store table name: {table!r}")
        self._engine_factory = engine_factory
        self._target: Any = None
        self._resolved = False
        self._table_name = table
        self._table_object: Table | None = None
        self._create_table = create_table
        self._ensured = False
        self._dialect: str | None = None
        self._guard = asyncio.Lock()

    def _datasource(self) -> Any:
        if not self._resolved:
            factory = self._engine_factory
            self._target = factory() if callable(factory) else factory
            self._resolved = True
        return self._target

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import users_table

            self._table_object = users_table(self._table_name)
        return self._table_object

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the users table when allowed, and check it (idempotent)."""
        await self._ensure_table()

    async def stop(self) -> None:
        """Nothing to release: the engine is not the store's. Idempotent."""

    async def _ensure_table(self) -> None:
        if self._ensured:
            return
        from pyfly.data.relational.framework_schema import ensure_tables

        async with self._guard:
            if self._ensured:
                return
            await ensure_tables(self._datasource(), self._table, create=self._create_table)
            self._ensured = True

    # ------------------------------------------------------------------
    # UserDetailsService
    # ------------------------------------------------------------------

    async def load_user_by_username(self, username: str) -> UserDetails | None:
        from sqlalchemy import select

        await self._ensure_table()
        table = self._table
        statement = select(
            table.c.username, table.c.password_hash, table.c.roles, table.c.permissions, table.c.enabled
        ).where(table.c.username == username)
        async with infrastructure_unit(self._datasource(), read_only=True) as session:
            row = (await session.execute(statement)).first()
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
        from pyfly.data.relational.framework_schema import framework_engine
        from pyfly.data.relational.upsert import backend_name, native_upsert, upsert

        await self._ensure_table()
        if self._dialect is None:
            self._dialect = backend_name(framework_engine(self._datasource()))
        values = {
            "username": user.username,
            "password_hash": user.password_hash,
            "roles": json.dumps(list(user.roles)),
            "permissions": json.dumps(list(user.permissions)),
            "enabled": 1 if user.enabled else 0,
        }
        async with infrastructure_unit(self._datasource(), single_statement=native_upsert(self._dialect)) as session:
            await upsert(session, self._table, values, key=["username"])

    async def delete(self, username: str) -> None:
        from sqlalchemy import delete

        await self._ensure_table()
        table = self._table
        async with infrastructure_unit(self._datasource(), single_statement=True) as session:
            await session.execute(delete(table).where(table.c.username == username))
