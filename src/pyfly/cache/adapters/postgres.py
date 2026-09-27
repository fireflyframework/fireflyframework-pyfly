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
"""SQL-backed cache adapter (``pyfly.cache.provider=postgres``).

The entries live in the framework table ``pyfly_cache_entries``
(:func:`~pyfly.data.relational.framework_schema.cache_entries_table`): the key, the serialized value and an
optional expiry. PostgreSQL is the provider's name and main target, and the adapter runs on every backend
SQLAlchemy supports: the table is a Core table with portable types and the upserts are the dialect's own
(:mod:`pyfly.data.relational.upsert`).

- **Expiry is an instant.** ``expires_at`` is a UTC instant bound as a typed value, so an entry expires
  after its TTL whatever the process's time zone, and nodes in different zones agree.
- **An expired key is free.** ``put_if_absent`` takes a key whose entry expired (one statement on
  PostgreSQL and SQLite: ``ON CONFLICT ... DO UPDATE ... WHERE expires_at <= now``), so it works as a
  lock or a dedupe marker with a TTL, as on Redis.
- **Expired rows are purged.** At most once per ``purge_interval`` a write purges expired rows, in batches,
  after its transaction commits; :meth:`PostgresCacheAdapter.purge_expired` does it on demand.
- **One round trip.** Every operation runs through :func:`~pyfly.data.transaction.infrastructure_unit`:
  outside a unit of work a single statement runs on an autocommit connection on PostgreSQL. Inside a unit
  on the cache's datasource it joins that unit, so an entry written by a transaction that rolls back is
  rolled back too. A read-only unit cannot write: a write made inside one gets a unit of its own.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.cache.serialization import cache_dumps, cache_loads
from pyfly.data.transaction import (
    Propagation,
    TransactionTemplate,
    after_commit,
    current_unit_of_work,
    infrastructure_unit,
    resolve_manager,
)

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.sql.elements import ColumnElement

_logger = logging.getLogger(__name__)

_LIKE_ESCAPE = "!"
"""The LIKE escape character: one every backend reads the same way in a string literal (a backslash is an
escape of its own in MySQL literals)."""

_UNBOUNDED_KEY_DIALECTS = frozenset({"postgresql", "sqlite"})
"""Where the key column is ``TEXT``; elsewhere it is ``VARCHAR(512)``."""

MAX_KEY_LENGTH = 512
"""The longest key the table takes where the key column needs a length (MySQL, MariaDB, SQL Server, Oracle)."""


def _glob_to_like(pattern: str) -> str:
    """Translate a glob pattern (``*`` / ``?``) to a SQL LIKE pattern (``%`` / ``_``), escaping with ``!``."""
    result: list[str] = []
    for ch in pattern:
        if ch == "*":
            result.append("%")
        elif ch == "?":
            result.append("_")
        elif ch in ("%", "_", _LIKE_ESCAPE):
            result.append(_LIKE_ESCAPE + ch)
        else:
            result.append(ch)
    return "".join(result)


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern matching every key that starts with *prefix* (taken literally)."""
    escaped = "".join(_LIKE_ESCAPE + ch if ch in ("%", "_", _LIKE_ESCAPE) else ch for ch in prefix)
    return escaped + "%"


class PostgresCacheAdapter:
    """Cache adapter backed by a SQL table (see the module documentation).

    Values are serialised to JSON bytes before storage (identical to the Redis adapter) so any
    JSON-compatible Python object can be cached transparently.

    Args:
        engine: where the entries live: an ``AsyncEngine``, a registry ``DataSource`` or a datasource name
            (the adapter does **not** dispose it in :meth:`stop`: it does not own it).
        create_table: create the table at :meth:`start` when it is missing (otherwise only check it).
        purge_interval: how often a write purges expired rows (``None``: only :meth:`purge_expired` does).
        table_name: the table (declared on the framework metadata under that name).
        clock: the current UTC instant (tests pass their own).
    """

    #: How many expired rows one purge statement deletes.
    PURGE_BATCH = 1000

    def __init__(
        self,
        engine: Any,
        *,
        create_table: bool = True,
        purge_interval: timedelta | None = timedelta(seconds=60),
        table_name: str = "pyfly_cache_entries",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = engine
        self._create_table = create_table
        self._purge_interval = purge_interval.total_seconds() if purge_interval is not None else None
        self._table_name = table_name
        self._clock = clock or (lambda: datetime.now(UTC))
        self._table_object: Table | None = None
        self._dialect: str | None = None
        self._last_purge = time.monotonic()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the cache table when allowed and check it (idempotent)."""
        from pyfly.data.relational.framework_schema import ensure_tables

        await ensure_tables(self._target, self._table, create=self._create_table)
        self._started = True

    async def stop(self) -> None:
        """Nothing to release: the engine belongs to the datasource registry (or to the caller). Idempotent."""

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    @property
    def engine(self) -> AsyncEngine:
        """The engine of the cache's datasource."""
        from pyfly.data.relational.framework_schema import framework_engine

        return framework_engine(self._target)

    @property
    def _table(self) -> Table:
        if self._table_object is None:
            from pyfly.data.relational.framework_schema import cache_entries_table

            self._table_object = cache_entries_table(self._table_name)
        return self._table_object

    @property
    def _backend(self) -> str:
        if self._dialect is None:
            from pyfly.data.relational.upsert import backend_name

            self._dialect = backend_name(self.engine)
        return self._dialect

    def _check_key(self, key: str) -> None:
        if len(key) > MAX_KEY_LENGTH and self._backend not in _UNBOUNDED_KEY_DIALECTS:
            raise ValueError(f"Cache keys are at most {MAX_KEY_LENGTH} characters on {self._backend}: {key[:40]!r}...")

    def _live(self, now: datetime) -> ColumnElement[bool]:
        from sqlalchemy import or_

        expires_at = self._table.c.expires_at
        return or_(expires_at.is_(None), expires_at > now)

    @contextlib.asynccontextmanager
    async def _writing(self, *, single_statement: bool) -> AsyncIterator[Any]:
        """The session a write runs on: the unit of work bound for the cache's datasource, or a short unit.

        A read-only unit refuses writes: a write made inside one runs in a unit of its own
        (``REQUIRES_NEW``), since a cache entry is not the read's work.
        """
        manager = resolve_manager(self._target)
        bound = current_unit_of_work(manager.datasource)
        if bound is not None and bound.read_only:
            async with TransactionTemplate(manager, propagation=Propagation.REQUIRES_NEW).transaction() as unit:
                assert unit is not None
                yield unit.resource
            return
        async with infrastructure_unit(manager, single_statement=single_statement) as session:
            yield session

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Serialise and upsert *value* with optional TTL."""
        from pyfly.data.relational.upsert import native_upsert, upsert

        await self._ensure_started()
        self._check_key(key)
        values = {
            "cache_key": key,
            "value": cache_dumps(value),
            "expires_at": self._clock() + ttl if ttl is not None else None,
        }
        async with self._writing(single_statement=native_upsert(self._backend)) as session:
            await upsert(session, self._table, values, key=["cache_key"])
        await self._purge_if_due()

    async def get(self, key: str) -> Any | None:
        """Retrieve and deserialise a cached value, honouring expiry."""
        from sqlalchemy import select

        await self._ensure_started()
        table = self._table
        statement = select(table.c.value).where(table.c.cache_key == key, self._live(self._clock()))
        async with infrastructure_unit(self._target, read_only=True) as session:
            raw = (await session.execute(statement)).scalar_one_or_none()

        if raw is None:
            self._misses += 1
            return None

        try:
            decoded = cache_loads(bytes(raw))
            self._hits += 1
            return decoded
        except (ValueError, TypeError):
            self._misses += 1
            _logger.warning("Failed to deserialise cached value for key '%s'", key)
            return None

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store *value* only if *key* is absent or its entry expired; return whether this call stored it.

        One statement on PostgreSQL and SQLite. Elsewhere the expired entry is taken over first, then the
        key inserted if absent, each in a short unit of its own when there is no unit of work to join (two
        callers racing for one key never deadlock on MySQL's gap locks).
        """
        from pyfly.data.relational.upsert import insert_if_absent, native_conditional_insert, take_over

        await self._ensure_started()
        self._check_key(key)
        now = self._clock()
        table = self._table
        values = {"cache_key": key, "value": cache_dumps(value), "expires_at": now + ttl if ttl is not None else None}
        expired = table.c.expires_at <= now
        if native_conditional_insert(self._backend):
            async with self._writing(single_statement=True) as session:
                stored = await insert_if_absent(session, table, values, key=["cache_key"], replace_where=expired)
        else:
            async with self._writing(single_statement=True) as session:
                stored = await take_over(session, table, values, key=["cache_key"], where=expired)
            if not stored:
                async with self._writing(single_statement=True) as session:
                    stored = await insert_if_absent(session, table, values, key=["cache_key"])
        await self._purge_if_due()
        return stored

    async def evict(self, key: str) -> bool:
        """Remove *key*. Returns ``True`` if the key existed."""
        from sqlalchemy import delete

        await self._ensure_started()
        table = self._table
        async with self._writing(single_statement=True) as session:
            result = await session.execute(delete(table).where(table.c.cache_key == key))
        existed = bool(result.rowcount > 0)
        if existed:
            self._evictions += 1
        return existed

    async def evict_by_prefix(self, prefix: str) -> int:
        """Delete every key starting with *prefix*. Returns the number deleted."""
        from sqlalchemy import delete

        await self._ensure_started()
        table = self._table
        statement = delete(table).where(table.c.cache_key.like(_like_prefix(prefix), escape=_LIKE_ESCAPE))
        async with self._writing(single_statement=True) as session:
            result = await session.execute(statement)
        count = int(result.rowcount)
        self._evictions += count
        return count

    async def exists(self, key: str) -> bool:
        """Return ``True`` iff *key* exists and has not expired."""
        from sqlalchemy import literal, select

        await self._ensure_started()
        table = self._table
        statement = select(literal(1)).where(table.c.cache_key == key, self._live(self._clock()))
        async with infrastructure_unit(self._target, read_only=True) as session:
            return (await session.execute(statement)).first() is not None

    async def clear(self) -> None:
        """Remove all entries from the cache table."""
        from sqlalchemy import delete

        await self._ensure_started()
        async with self._writing(single_statement=True) as session:
            await session.execute(delete(self._table))

    # ------------------------------------------------------------------
    # Expired rows
    # ------------------------------------------------------------------

    async def purge_expired(self) -> int:
        """Delete every expired entry, :attr:`PURGE_BATCH` rows per statement; return how many were deleted."""
        from sqlalchemy import delete, select

        await self._ensure_started()
        table = self._table
        now = self._clock()
        expired = table.c.expires_at <= now
        if self._backend in ("mysql", "mariadb"):
            statement: Any = delete(table).where(expired).with_dialect_options(mysql_limit=self.PURGE_BATCH)
        elif self._backend in _UNBOUNDED_KEY_DIALECTS:
            batch = select(table.c.cache_key).where(expired).limit(self.PURGE_BATCH)
            statement = delete(table).where(table.c.cache_key.in_(batch))
        else:
            statement = delete(table).where(expired)
        purged = 0
        while True:
            async with self._writing(single_statement=True) as session:
                deleted = int((await session.execute(statement)).rowcount)
            purged += deleted
            if deleted < self.PURGE_BATCH:
                return purged

    async def _purge_if_due(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        # After the write's transaction commits (at once when there is none): the purge is not the caller's
        # work and never joins its unit.
        await after_commit(self._purge_quietly, datasource=resolve_manager(self._target).datasource)

    async def _purge_quietly(self) -> None:
        try:
            purged = await self.purge_expired()
        except Exception:  # noqa: BLE001 — a failed purge must never fail a cache write
            _logger.warning("cache_purge_failed", extra={"table": self._table_name}, exc_info=True)
            return
        if purged:
            _logger.debug("cache_expired_entries_purged", extra={"table": self._table_name, "count": purged})

    # ------------------------------------------------------------------
    # Extended operations (beyond the Protocol minimum)
    # ------------------------------------------------------------------

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Return up to *limit* non-expired keys matching the glob *pattern*."""
        from sqlalchemy import select

        await self._ensure_started()
        table = self._table
        statement = (
            select(table.c.cache_key)
            .where(table.c.cache_key.like(_glob_to_like(pattern), escape=_LIKE_ESCAPE), self._live(self._clock()))
            .limit(limit)
        )
        async with infrastructure_unit(self._target, read_only=True) as session:
            return [str(key) for key in (await session.execute(statement)).scalars().all()]

    async def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate."""
        from sqlalchemy import func, select

        await self._ensure_started()
        statement = select(func.count()).select_from(self._table).where(self._live(self._clock()))
        async with infrastructure_unit(self._target, read_only=True) as session:
            size = int((await session.execute(statement)).scalar() or 0)

        requests = self._hits + self._misses
        return {
            "size": size,
            "type": "postgres",
            "requests": requests,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / requests) if requests else 0.0,
        }
