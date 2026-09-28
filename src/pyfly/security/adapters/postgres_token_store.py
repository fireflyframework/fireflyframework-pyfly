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
"""SQL table-backed OAuth2 token store (``pyfly.security.oauth2.token-store.provider=postgres``).

Durable, auditable storage of refresh tokens, authorization codes, rotation families and pushed
authorization requests, shared by every instance of a multi-instance authorization server, with no Redis
required. PostgreSQL is the provider's name; the store runs on every backend SQLAlchemy supports, on the
framework tables ``pyfly_oauth2_grants`` and ``pyfly_oauth2_token_families``
(:func:`~pyfly.data.relational.framework_schema.oauth2_grants_table`).

- **Atomic grants.** The store is an :class:`~pyfly.security.oauth2.authorization_server.AtomicTokenStore`:
  each grant is one unit of work of conditional statements over typed columns, so a code or refresh token
  is consumed once (``UPDATE ... SET used = true WHERE used = false AND expires_at >= :now``, one row or
  none) however many requests or instances present it, and a grant that fails midway changes nothing.
- **Families that stay revoked.** A family is a row whose ``active`` only goes from true to false. A rotation
  locks it first (``UPDATE ... WHERE active``) and mints its token only while it is active; a revocation
  locks it, deactivates it and deletes its tokens with one statement each. Both take the family's row
  before any token's, so they queue on each other and cannot deadlock, and a revocation is never undone.
- **A unit of its own.** No operation joins a unit of work of its caller: a grant is a security decision the
  client acts on at once, so a caller's rollback must not resurrect a used code or a revoked family.
- **Purged.** At most once per *purge_interval* a write deletes, a batch per table, what expired more than
  *purge_grace* ago (the grace keeps a used token long enough for a late replay to still revoke its
  family); :meth:`PostgresTokenStore.purge_expired` deletes it all.

Records written by an earlier release (the ``pyfly_oauth2_tokens`` table of JSON blobs) are not read: the
clients those tokens were issued to authenticate again. Drop that table once the release is out.

Hexagonal: the datasource is injected by the composition root; this module imports no SQLAlchemy at module
scope.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.data.transaction import infrastructure_unit, outside_transaction
from pyfly.security.oauth2.authorization_server import (
    AUTHORIZATION_CODE,
    REFRESH_TOKEN,
    GrantOutcome,
    TokenRecord,
)

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

_logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _instant(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _seconds(instant: datetime) -> int:
    return int(instant.timestamp())


class PostgresTokenStore:
    """OAuth2 token store on two SQL tables (see the module documentation).

    Args:
        engine_factory: The store's datasource (an ``AsyncEngine``, a registry ``DataSource`` or a datasource
            name) or a zero-argument callable returning it, resolved once, at first use. The store does not
            dispose it.
        table: The table of refresh tokens, codes and pushed requests.
        families_table: The table of rotation families.
        create_table: Create the tables at :meth:`start` when they are missing (otherwise only check them).
        purge_interval: How often a write purges expired records (``None``: only :meth:`purge_expired` does).
        purge_grace: How long an expired record is kept before a purge deletes it.
        clock: The current UTC instant, for the purge (tests pass their own).
    """

    #: How many expired rows one purge statement deletes.
    PURGE_BATCH = 1000

    def __init__(
        self,
        engine_factory: Callable[[], Any] | Any,
        *,
        table: str = "pyfly_oauth2_grants",
        families_table: str = "pyfly_oauth2_token_families",
        create_table: bool = True,
        purge_interval: timedelta | None = timedelta(seconds=60),
        purge_grace: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name in (table, families_table):
            if not _IDENT.match(name):
                raise ValueError(f"Invalid token-store table name: {name!r}")
        self._engine_factory = engine_factory
        self._target: Any = None
        self._resolved = False
        self._table_name = table
        self._families_table_name = families_table
        self._tables: tuple[Table, Table] | None = None
        self._create_table = create_table
        self._purge_interval = purge_interval.total_seconds() if purge_interval is not None else None
        self.purge_grace = purge_grace
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_purge = time.monotonic()
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
        """The engine of the store's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._datasource())

    def _both(self) -> tuple[Table, Table]:
        if self._tables is None:
            from pyfly.data.relational.framework_schema import oauth2_grants_table, oauth2_token_families_table

            self._tables = (
                oauth2_grants_table(self._table_name),
                oauth2_token_families_table(self._families_table_name),
            )
        return self._tables

    @contextlib.asynccontextmanager
    async def _unit(self, *, read_only: bool = False, single_statement: bool = False) -> AsyncIterator[AsyncSession]:
        """A unit of work of the store's own, whatever units the caller has open (module documentation)."""
        await self.start()
        with outside_transaction():
            async with infrastructure_unit(
                self._datasource(), read_only=read_only, single_statement=single_statement
            ) as session:
                yield session

    def _row(self, record: TokenRecord) -> dict[str, Any]:
        return {
            "token_id": record.token_id,
            "kind": record.kind,
            "client_id": record.client_id,
            "family_id": record.family_id,
            "used": record.used,
            "expires_at": _instant(record.expires_at),
            "data": json.dumps(record.data),
        }

    # ------------------------------------------------------------------
    # AtomicTokenStore
    # ------------------------------------------------------------------

    async def save(self, record: TokenRecord) -> None:
        """Store a new authorization code or pushed authorization request (one ``INSERT``)."""
        from sqlalchemy import insert

        grants, _ = self._both()
        async with self._unit(single_statement=True) as session:
            await session.execute(insert(grants).values(self._row(record)))
        await self._purge_if_due()

    async def load(self, kind: str, token_id: str) -> TokenRecord | None:
        """The record, with whether its family is active (one ``SELECT``, a family row that is gone counts as
        revoked)."""
        from sqlalchemy import select

        grants, families = self._both()
        statement = (
            select(
                grants.c.client_id,
                grants.c.family_id,
                grants.c.used,
                grants.c.expires_at,
                grants.c.data,
                families.c.active,
            )
            .select_from(grants.outerjoin(families, families.c.family_id == grants.c.family_id))
            .where(grants.c.token_id == token_id, grants.c.kind == kind)
        )
        async with self._unit(read_only=True, single_statement=True) as session:
            row = (await session.execute(statement)).first()
        if row is None:
            return None
        return TokenRecord(
            token_id=token_id,
            kind=kind,
            client_id=row.client_id,
            expires_at=_seconds(row.expires_at),
            data=json.loads(row.data),
            family_id=row.family_id,
            used=bool(row.used),
            family_active=row.family_id is None or bool(row.active),
        )

    async def issue(self, token: TokenRecord) -> None:
        """Store *token* and open its family, in one unit."""
        from sqlalchemy import insert

        grants, families = self._both()
        async with self._unit() as session:
            await session.execute(insert(families).values(self._family_row(token)))
            await session.execute(insert(grants).values(self._row(token)))
        await self._purge_if_due()

    async def redeem(self, code: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        """Consume *code* with one conditional ``UPDATE`` (which records the family it issues), then open the
        family and store *token*, in one unit. A code consumed already revokes that family."""
        from sqlalchemy import false, insert, select, update

        grants, families = self._both()
        async with self._unit() as session:
            consumed = await session.execute(
                update(grants)
                .where(
                    grants.c.token_id == code,
                    grants.c.kind == AUTHORIZATION_CODE,
                    grants.c.used == false(),
                    grants.c.expires_at >= _instant(now),
                )
                .values(used=True, family_id=token.family_id)
            )
            if _rowcount(consumed) == 1:
                await session.execute(insert(families).values(self._family_row(token)))
                await session.execute(insert(grants).values(self._row(token)))
                outcome = GrantOutcome.GRANTED
            else:
                current = (
                    await session.execute(
                        select(grants.c.used, grants.c.family_id)
                        .where(grants.c.token_id == code, grants.c.kind == AUTHORIZATION_CODE)
                        .with_for_update()
                    )
                ).first()
                if current is None:
                    outcome = GrantOutcome.UNKNOWN
                elif current.used:
                    if current.family_id is not None:
                        await self._revoke(session, current.family_id)
                    outcome = GrantOutcome.REPLAYED
                else:
                    outcome = GrantOutcome.EXPIRED
        await self._purge_if_due()
        return outcome

    async def rotate(self, token_id: str, token: TokenRecord, *, now: int) -> GrantOutcome:
        """Lock the family while it is active (extending its expiry), consume *token_id* with one conditional
        ``UPDATE``, and store *token*, in one unit. A token consumed already revokes the family."""
        from sqlalchemy import case, false, insert, select, true, update

        grants, families = self._both()
        family_id = token.family_id
        until = _instant(token.expires_at)
        async with self._unit() as session:
            # The family's row first: rotations and revocations of one family queue on it.
            locked = await session.execute(
                update(families)
                .where(families.c.family_id == family_id, families.c.active == true())
                .values(expires_at=case((families.c.expires_at < until, until), else_=families.c.expires_at))
            )
            if _rowcount(locked) != 1:
                return GrantOutcome.REVOKED
            consumed = await session.execute(
                update(grants)
                .where(
                    grants.c.token_id == token_id,
                    grants.c.kind == REFRESH_TOKEN,
                    grants.c.family_id == family_id,
                    grants.c.used == false(),
                    grants.c.expires_at >= _instant(now),
                )
                .values(used=True)
            )
            if _rowcount(consumed) == 1:
                await session.execute(insert(grants).values(self._row(token)))
                outcome = GrantOutcome.GRANTED
            else:
                current = (
                    await session.execute(
                        select(grants.c.used)
                        .where(
                            grants.c.token_id == token_id,
                            grants.c.kind == REFRESH_TOKEN,
                            grants.c.family_id == family_id,
                        )
                        .with_for_update()
                    )
                ).first()
                if current is None:
                    outcome = GrantOutcome.UNKNOWN
                elif current.used:
                    await self._revoke(session, str(family_id))
                    outcome = GrantOutcome.REPLAYED
                else:
                    outcome = GrantOutcome.EXPIRED
        await self._purge_if_due()
        return outcome

    async def take(self, kind: str, token_id: str, *, client_id: str, now: int) -> TokenRecord | None:
        """Delete and return the unexpired record of *client_id*: one ``DELETE ... RETURNING`` where the
        dialect has it, else a ``SELECT`` and a ``DELETE`` whose row count says who took it."""
        from sqlalchemy import delete, select

        grants, _ = self._both()
        criteria = (
            grants.c.token_id == token_id,
            grants.c.kind == kind,
            grants.c.client_id == client_id,
            grants.c.expires_at >= _instant(now),
        )
        columns = (grants.c.expires_at, grants.c.data, grants.c.family_id, grants.c.used)
        await self.start()  # the dialect knows what the server supports once it has connected
        returning = bool(getattr(self.engine.dialect, "delete_returning", False))
        async with self._unit(single_statement=returning) as session:
            if returning:
                row = (await session.execute(delete(grants).where(*criteria).returning(*columns))).first()
            else:
                row = (await session.execute(select(*columns).where(*criteria))).first()
                if row is not None:
                    deleted = await session.execute(
                        delete(grants).where(grants.c.token_id == token_id, grants.c.kind == kind)
                    )
                    if _rowcount(deleted) != 1:
                        row = None  # a concurrent request took it
        if row is None:
            return None
        return TokenRecord(
            token_id=token_id,
            kind=kind,
            client_id=client_id,
            expires_at=_seconds(row.expires_at),
            data=json.loads(row.data),
            family_id=row.family_id,
            used=bool(row.used),
        )

    async def revoke_family(self, family_id: str) -> None:
        """Deactivate the family and delete its refresh tokens: two statements, whatever the family's length."""
        async with self._unit() as session:
            await self._revoke(session, family_id)

    # ------------------------------------------------------------------
    # Expired records
    # ------------------------------------------------------------------

    async def purge_expired(self) -> int:
        """Delete every record and family that expired more than :attr:`purge_grace` ago,
        :attr:`PURGE_BATCH` rows per statement; return how many were deleted."""
        cutoff = self._clock() - self.purge_grace
        purged = 0
        for table in self._both():
            while True:
                deleted = await self._purge_batch(table, cutoff)
                purged += deleted
                if deleted < self.PURGE_BATCH:
                    break
        return purged

    async def _purge_batch(self, table: Table, cutoff: datetime) -> int:
        from sqlalchemy import delete, select

        expired = table.c.expires_at < cutoff
        backend = self._backend()
        if backend in ("mysql", "mariadb"):
            # Both spellings: a mariadb:// URL's dialect reads only mariadb_limit, a mysql:// one mysql_limit.
            limit = {"mysql_limit": self.PURGE_BATCH, "mariadb_limit": self.PURGE_BATCH}
            statement: Any = delete(table).where(expired).with_dialect_options(**limit)
        elif backend in ("postgresql", "sqlite"):
            key = next(iter(table.primary_key.columns))
            statement = delete(table).where(key.in_(select(key).where(expired).limit(self.PURGE_BATCH)))
        else:
            statement = delete(table).where(expired)
        async with self._unit(single_statement=True) as session:
            return _rowcount(await session.execute(statement))

    async def _purge_if_due(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        try:
            purged = 0
            for table in self._both():
                purged += await self._purge_batch(table, self._clock() - self.purge_grace)
        except Exception:  # noqa: BLE001 — a failed purge must never fail a grant
            _logger.warning("oauth2_token_purge_failed", extra={"table": self._table_name}, exc_info=True)
            return
        if purged >= self.PURGE_BATCH:
            self._last_purge = float("-inf")  # a backlog: the next write purges the next batch
        if purged:
            _logger.debug("oauth2_tokens_purged", extra={"table": self._table_name, "count": purged})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _backend(self) -> str:
        if self._dialect is None:
            from pyfly.data.relational.upsert import backend_name

            self._dialect = backend_name(self.engine)
        return self._dialect

    def _family_row(self, token: TokenRecord) -> dict[str, Any]:
        return {
            "family_id": token.family_id,
            "client_id": token.client_id,
            "active": True,
            "expires_at": _instant(token.expires_at),
        }

    async def _revoke(self, session: AsyncSession, family_id: str) -> None:
        from sqlalchemy import delete, update

        grants, families = self._both()
        await session.execute(update(families).where(families.c.family_id == family_id).values(active=False))
        await session.execute(delete(grants).where(grants.c.family_id == family_id, grants.c.kind == REFRESH_TOKEN))


def _rowcount(result: Any) -> int:
    """The rows a DML statement matched (``CursorResult.rowcount``)."""
    return int(result.rowcount)
