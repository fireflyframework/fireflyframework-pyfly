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
"""SQL table-backed :class:`~pyfly.session.concurrency.SessionRegistry` adapter
(``pyfly.session.concurrency.registry=postgres``).

Durable, queryable, cross-process session concurrency control, with no Redis required. PostgreSQL is the
provider's name; the registry runs on every backend SQLAlchemy supports, on the framework tables
``pyfly_session_registrations`` (a row per session: principal, creation, next liveness check) and
``pyfly_session_principals`` (a row per principal).

- **An atomic cap.** :meth:`PostgresSessionRegistry.register_limited` locks the principal's row
  (``UPDATE ... SET version = version + 1``), then counts, evicts and registers in the same unit of work: the
  logins of one principal take turns, on every instance, so max-sessions holds under concurrency.
- **Purged.** A registration is due for a liveness check one *ttl* after it was registered or last renewed;
  the controller's purge (``SessionConcurrencyController.purge_expired``, also run by logins) drops those whose
  session the store no longer has and renews the others.
- **A unit of its own.** No operation joins a unit of work of its caller.

Evicting a session another instance holds ends it only when the session store is shared too: pair the
registry with ``pyfly.session.store=postgres`` (:class:`~pyfly.session.adapters.sql_session_store.SqlSessionStore`)
or ``redis``. Registrations written by an earlier release (the ``pyfly_session_registry`` table) are not read.

Hexagonal: the datasource is injected by the composition root; this module imports no SQLAlchemy at module
scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import infrastructure_unit, outside_transaction
from pyfly.session.concurrency import SessionRegistration, plan_registration

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# Guard against SQL injection via a misconfigured table name.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PostgresSessionRegistry:
    """Per-principal session index in SQL tables (see the module documentation).

    Args:
        engine_factory: The registry's datasource (an ``AsyncEngine``, a registry ``DataSource`` or a
            datasource name) or a zero-argument callable returning it, resolved once, at first use.
        table: The table of registrations.
        principals_table: The table of principals (the rows a capped login locks).
        ttl: How long a registration goes before its liveness is checked again (the session timeout, by
            default ``pyfly.session.ttl``); seconds or a ``timedelta``, positive (``ValueError`` otherwise).
        create_table: Create the tables at :meth:`start` when they are missing (otherwise only check them).
        clock: The current UTC instant (tests pass their own).
    """

    def __init__(
        self,
        engine_factory: Callable[[], Any] | Any,
        *,
        table: str = "pyfly_session_registrations",
        principals_table: str = "pyfly_session_principals",
        ttl: timedelta | float = timedelta(seconds=1800),
        create_table: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name in (table, principals_table):
            if not _IDENT.match(name):
                raise ValueError(f"Invalid session-registry table name: {name!r}")
        self._engine_factory = engine_factory
        self._target: Any = None
        self._resolved = False
        self._table_name = table
        self._principals_table_name = principals_table
        self._tables: tuple[Table, Table] | None = None
        self._ttl = ttl if isinstance(ttl, timedelta) else timedelta(seconds=float(ttl))
        if self._ttl <= timedelta(0):
            # A renewal would leave the registration due: the purge would check the same batch forever.
            raise ValueError(f"The session-registry ttl must be positive, got {self._ttl}")
        self._create_table = create_table
        self._clock = clock or (lambda: datetime.now(UTC))
        self._dialect: str | None = None
        self._started = False
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle and wiring
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the tables when allowed, and check them (idempotent)."""
        if self._started:
            return
        from pyfly.data.relational.framework_schema import ensure_tables

        async with self._guard:
            if self._started:
                return
            await ensure_tables(self._datasource(), *self._both(), create=self._create_table)
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
        """The engine of the registry's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._datasource())

    def _both(self) -> tuple[Table, Table]:
        if self._tables is None:
            from pyfly.data.relational.framework_schema import session_principals_table, session_registrations_table

            self._tables = (
                session_registrations_table(self._table_name),
                session_principals_table(self._principals_table_name),
            )
        return self._tables

    def _backend(self) -> str:
        if self._dialect is None:
            from pyfly.data.relational.upsert import backend_name

            self._dialect = backend_name(self.engine)
        return self._dialect

    @contextlib.asynccontextmanager
    async def _unit(self, *, read_only: bool = False, single_statement: bool = False) -> AsyncIterator[AsyncSession]:
        await self.start()
        with outside_transaction():
            async with infrastructure_unit(
                self._datasource(), read_only=read_only, single_statement=single_statement
            ) as session:
                yield session

    def _values(self, principal: str, session_id: str, created_at: float) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "principal": principal,
            "created_at": datetime.fromtimestamp(created_at, UTC),
            "expires_at": self._clock() + self._ttl,
        }

    # ------------------------------------------------------------------
    # SessionRegistry
    # ------------------------------------------------------------------

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        from pyfly.data.relational.upsert import native_upsert, upsert

        registrations, _ = self._both()
        async with self._unit(single_statement=native_upsert(self._backend())) as session:
            await upsert(session, registrations, self._values(principal, session_id, created_at), key=["session_id"])

    async def deregister(self, principal: str, session_id: str) -> None:
        from sqlalchemy import delete

        registrations, _ = self._both()
        async with self._unit(single_statement=True) as session:
            await session.execute(
                delete(registrations).where(
                    registrations.c.principal == principal, registrations.c.session_id == session_id
                )
            )

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        async with self._unit(read_only=True, single_statement=True) as session:
            return await self._sessions_of(session, principal)

    async def count(self, principal: str) -> int:
        from sqlalchemy import func, select

        registrations, _ = self._both()
        statement = select(func.count()).select_from(registrations).where(registrations.c.principal == principal)
        async with self._unit(read_only=True, single_statement=True) as session:
            return int((await session.execute(statement)).scalar() or 0)

    async def _sessions_of(
        self, session: AsyncSession, principal: str, *, besides: str | None = None
    ) -> list[tuple[str, float]]:
        from sqlalchemy import select

        registrations, _ = self._both()
        statement = (
            select(registrations.c.session_id, registrations.c.created_at)
            .where(registrations.c.principal == principal)
            .order_by(registrations.c.created_at, registrations.c.session_id)  # oldest first
        )
        if besides is not None:
            statement = statement.where(registrations.c.session_id != besides)
        rows = (await session.execute(statement)).all()
        return [(row.session_id, row.created_at.timestamp()) for row in rows]

    # ------------------------------------------------------------------
    # AtomicSessionRegistry
    # ------------------------------------------------------------------

    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration:
        """Count, evict and register in one unit of work that holds the principal's row (module
        documentation). The principal's first capped login inserts that row first, in a short unit of its
        own (a conditional insert beside the lock would deadlock MySQL's concurrent first logins)."""
        result = await self._register_locked(principal, session_id, created_at, max_sessions, evict_oldest)
        if result is None:
            await self._add_principal(principal)
            result = await self._register_locked(principal, session_id, created_at, max_sessions, evict_oldest)
        if result is None:  # the principal rows are never deleted: this cannot happen twice in a row
            raise RuntimeError(f"The session registry has no row for principal {principal!r}")
        return result

    async def _register_locked(
        self, principal: str, session_id: str, created_at: float, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration | None:
        from sqlalchemy import delete, update

        from pyfly.data.relational.upsert import upsert

        registrations, principals = self._both()
        async with self._unit() as session:
            locked = await session.execute(
                update(principals).where(principals.c.principal == principal).values(version=principals.c.version + 1)
            )
            if _rowcount(locked) != 1:
                return None
            existing = await self._sessions_of(session, principal, besides=session_id)
            accepted, evicted = plan_registration(existing, max_sessions=max_sessions, evict_oldest=evict_oldest)
            if not accepted:
                return SessionRegistration(False)
            if evicted:
                await session.execute(
                    delete(registrations).where(
                        registrations.c.principal == principal, registrations.c.session_id.in_(evicted)
                    )
                )
            await upsert(session, registrations, self._values(principal, session_id, created_at), key=["session_id"])
            return SessionRegistration(True, tuple(evicted))

    async def _add_principal(self, principal: str) -> None:
        from pyfly.data.relational.upsert import insert_if_absent

        _, principals = self._both()
        async with self._unit(single_statement=True) as session:
            await insert_if_absent(session, principals, {"principal": principal, "version": 0}, key=["principal"])

    # ------------------------------------------------------------------
    # ExpiringSessionRegistry
    # ------------------------------------------------------------------

    async def expired_sessions(self, *, limit: int) -> list[tuple[str, str]]:
        """Up to *limit* ``(principal, session_id)`` registrations due for a liveness check, most overdue
        first."""
        from sqlalchemy import select

        registrations, _ = self._both()
        statement = (
            select(registrations.c.principal, registrations.c.session_id)
            .where(registrations.c.expires_at <= self._clock())
            .order_by(registrations.c.expires_at, registrations.c.session_id)
            .limit(limit)
        )
        async with self._unit(read_only=True, single_statement=True) as session:
            return [(row.principal, row.session_id) for row in (await session.execute(statement)).all()]

    async def renew(self, session_ids: Sequence[str]) -> None:
        """Push the next liveness check of *session_ids* one *ttl* away."""
        from sqlalchemy import update

        if not session_ids:
            return
        registrations, _ = self._both()
        statement = (
            update(registrations)
            .where(registrations.c.session_id.in_(list(session_ids)))
            .values(expires_at=self._clock() + self._ttl)
        )
        async with self._unit(single_statement=True) as session:
            await session.execute(statement)


def _rowcount(result: Any) -> int:
    """The rows a DML statement matched (``CursorResult.rowcount``)."""
    return int(result.rowcount)
