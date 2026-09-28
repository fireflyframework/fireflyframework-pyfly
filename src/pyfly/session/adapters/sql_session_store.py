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
"""SQL table-backed :class:`~pyfly.session.ports.outbound.SessionStore` (``pyfly.session.store=postgres``).

HTTP sessions shared by every instance of the application in its relational database, with no Redis
required: Spring Session JDBC's place. With ``pyfly.session.concurrency.registry=postgres`` it makes the
session cap hold across instances, since evicting a session deletes it for every instance. PostgreSQL is the
provider's name; the store runs on every backend SQLAlchemy supports, on the framework table
``pyfly_sessions`` (:func:`~pyfly.data.relational.framework_schema.sessions_table`).

- **Attributes as JSON**, with the Redis store's encoding: a dataclass attribute (the ``SecurityContext`` a
  login stores) is tagged and rebuilt on read only if its type is allowlisted
  (:func:`~pyfly.session.adapters.redis.allow_session_type` covers both stores).
- **Expiry is an instant.** ``expires_at`` is a UTC instant: a session is read only before it, whatever the
  process's time zone.
- **Replaced only while held.** :meth:`SqlSessionStore.replace` (the
  :class:`~pyfly.session.ports.outbound.ConditionalSessionStore` operation) is one conditional ``UPDATE``
  that writes over a session only while the table holds it unexpired, so the ``SessionFilter`` never brings
  back a session that was logged out, evicted or expired while one of its requests ran.
- **Purged.** At most once per *purge_interval* a write deletes a batch of expired sessions, after its
  commit; :meth:`SqlSessionStore.purge_expired` deletes them all.
- **Joins the unit of work** bound for its datasource, as the other framework stores: outside one each
  operation is one statement (on an autocommit connection on PostgreSQL).

Hexagonal: the datasource is injected by the composition root; this module imports no SQLAlchemy at module
scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from pyfly.data.transaction import infrastructure_unit
from pyfly.session.adapters.redis import _json_default, _json_object_hook

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine

_logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SqlSessionStore:
    """Session store on a SQL table (see the module documentation).

    Args:
        engine_factory: The store's datasource (an ``AsyncEngine``, a registry ``DataSource`` or a datasource
            name) or a zero-argument callable returning it, resolved once, at first use.
        table: The sessions table.
        create_table: Create the table at :meth:`start` when it is missing (otherwise only check it).
        purge_interval: How often a write purges expired sessions (``None``: only :meth:`purge_expired`).
        clock: The current UTC instant (tests pass their own).
    """

    #: How many expired sessions one purge statement deletes.
    PURGE_BATCH = 1000

    def __init__(
        self,
        engine_factory: Callable[[], Any] | Any,
        *,
        table: str = "pyfly_sessions",
        create_table: bool = True,
        purge_interval: timedelta | None = timedelta(seconds=60),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _IDENT.match(table):
            raise ValueError(f"Invalid session-store table name: {table!r}")
        self._engine_factory = engine_factory
        self._target: Any = None
        self._resolved = False
        self._table_name = table
        self._table_object: Table | None = None
        self._create_table = create_table
        self._purge_interval = purge_interval.total_seconds() if purge_interval is not None else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_purge = time.monotonic()
        self._dialect: str | None = None
        self._started = False
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle and wiring
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the table when allowed, and check it (idempotent)."""
        if self._started:
            return
        from pyfly.data.relational.framework_schema import ensure_tables

        async with self._guard:
            if self._started:
                return
            await ensure_tables(self._datasource(), self._table, create=self._create_table)
            self._started = True

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller). Idempotent."""

    def _datasource(self) -> Any:
        if not self._resolved:
            factory = self._engine_factory
            self._target = factory() if callable(factory) else factory
            self._resolved = True
        return self._target

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the store's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._datasource())

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import sessions_table

            self._table_object = sessions_table(self._table_name)
        return self._table_object

    def _backend(self) -> str:
        if self._dialect is None:
            from pyfly.data.relational.upsert import backend_name

            self._dialect = backend_name(self.engine)
        return self._dialect

    # ------------------------------------------------------------------
    # SessionStore
    # ------------------------------------------------------------------

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """The session's attributes, or ``None`` when it is missing or expired."""
        from sqlalchemy import select

        await self.start()
        table = self._table
        statement = select(table.c.data).where(table.c.session_id == session_id, table.c.expires_at > self._clock())
        async with infrastructure_unit(self._datasource(), read_only=True, single_statement=True) as session:
            raw = (await session.execute(statement)).scalar_one_or_none()
        if raw is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(raw, object_hook=_json_object_hook))
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Failed to deserialize session '%s'", session_id)
            return None

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        """Insert or replace the session, expiring *ttl* seconds from now (one statement)."""
        from pyfly.data.relational.upsert import native_upsert, upsert

        await self.start()
        values = {
            "session_id": session_id,
            "data": json.dumps(data, default=_json_default),
            "expires_at": self._clock() + timedelta(seconds=ttl),
        }
        async with infrastructure_unit(self._datasource(), single_statement=native_upsert(self._backend())) as session:
            await upsert(session, self._table, values, key=["session_id"])
        await self._purge_if_due()

    async def replace(self, session_id: str, data: dict[str, Any], ttl: int) -> bool:
        """Replace the session's attributes and expire it *ttl* seconds from now, only while the table holds it
        unexpired: one conditional ``UPDATE`` (a matched row counts on MySQL and MariaDB too, whose dialects
        report matched rows). ``False`` when the session is gone (deleted or expired), and nothing is written."""
        from sqlalchemy import update

        await self.start()
        table = self._table
        now = self._clock()
        statement = (
            update(table)
            .where(table.c.session_id == session_id, table.c.expires_at > now)
            .values(data=json.dumps(data, default=_json_default), expires_at=now + timedelta(seconds=ttl))
        )
        async with infrastructure_unit(self._datasource(), single_statement=True) as session:
            replaced = int((await session.execute(statement)).rowcount) == 1
        if replaced:
            await self._purge_if_due()
        return replaced

    async def delete(self, session_id: str) -> None:
        from sqlalchemy import delete

        await self.start()
        table = self._table
        async with infrastructure_unit(self._datasource(), single_statement=True) as session:
            await session.execute(delete(table).where(table.c.session_id == session_id))

    async def exists(self, session_id: str) -> bool:
        """Whether the session is there and has not expired."""
        from sqlalchemy import literal, select

        await self.start()
        table = self._table
        statement = select(literal(1)).where(table.c.session_id == session_id, table.c.expires_at > self._clock())
        async with infrastructure_unit(self._datasource(), read_only=True, single_statement=True) as session:
            return (await session.execute(statement)).first() is not None

    # ------------------------------------------------------------------
    # Expired sessions
    # ------------------------------------------------------------------

    async def purge_expired(self) -> int:
        """Delete every expired session, :attr:`PURGE_BATCH` per statement; return how many were deleted."""
        await self.start()
        purged = 0
        while True:
            deleted = await self._purge_batch()
            purged += deleted
            if deleted < self.PURGE_BATCH:
                return purged

    async def _purge_batch(self) -> int:
        from sqlalchemy import delete, select

        table = self._table
        expired = table.c.expires_at <= self._clock()
        backend = self._backend()
        if backend in ("mysql", "mariadb"):
            # Both spellings: a mariadb:// URL's dialect reads only mariadb_limit, a mysql:// one mysql_limit.
            limit = {"mysql_limit": self.PURGE_BATCH, "mariadb_limit": self.PURGE_BATCH}
            statement: Any = delete(table).where(expired).with_dialect_options(**limit)
        elif backend in ("postgresql", "sqlite"):
            batch = select(table.c.session_id).where(expired).limit(self.PURGE_BATCH)
            statement = delete(table).where(table.c.session_id.in_(batch))
        else:
            statement = delete(table).where(expired)
        async with infrastructure_unit(self._datasource(), single_statement=True) as session:
            return int((await session.execute(statement)).rowcount)

    async def _purge_if_due(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        from pyfly.data.transaction import after_commit, resolve_manager

        # After the write's transaction commits (at once when there is none): the purge is not the caller's
        # work and never joins its unit.
        await after_commit(self._purge_quietly, datasource=resolve_manager(self._datasource()).datasource)

    async def _purge_quietly(self) -> None:
        try:
            purged = await self._purge_batch()
        except Exception:  # noqa: BLE001 — a failed purge must never fail a session write
            _logger.warning("session_purge_failed", extra={"table": self._table_name}, exc_info=True)
            return
        if purged >= self.PURGE_BATCH:
            self._last_purge = float("-inf")  # a backlog: the next write purges the next batch
        if purged:
            _logger.debug("sessions_purged", extra={"table": self._table_name, "count": purged})
