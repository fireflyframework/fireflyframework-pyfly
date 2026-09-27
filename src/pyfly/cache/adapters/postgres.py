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
- **Expired rows are purged.** At most once per ``purge_interval`` a write purges one batch of expired rows
  (:attr:`PostgresCacheAdapter.PURGE_BATCH`) after its transaction commits, and while a batch comes back
  full the next write purges the next one, so a backlog is worked off without ever landing on one request;
  :meth:`PostgresCacheAdapter.purge_expired` deletes them all on demand.
- **Its own namespace.** The entries live under a key prefix, *namespace* (``pyfly:cache:`` by default), as
  on Redis: keys passed to and returned by the adapter are the cache's own (``product:1`` is stored as
  ``pyfly:cache:product:1``), :meth:`PostgresCacheAdapter.clear` deletes the namespace only, never the whole
  table, and :meth:`PostgresCacheAdapter.with_namespace` gives a cache of its own on the same table that the
  root cache's ``clear()`` never touches. An empty namespace declares that the cache owns the whole table.
  Where the key column has a length (MySQL, MariaDB, SQL Server, Oracle), a key is at most
  :data:`MAX_KEY_LENGTH` characters *with* the namespace, and a longer one is refused with ``ValueError``
  before anything is written (the CQRS query cache and the cache decorators log it and do not cache).
- **One round trip.** Every operation runs through :func:`~pyfly.data.transaction.infrastructure_unit`:
  outside a unit of work a single statement runs on an autocommit connection on PostgreSQL.

Called directly inside a unit of work on the cache's datasource, an operation joins that unit, so an entry
written by a transaction that rolls back is rolled back too (a read-only unit cannot write: a write made
inside one gets a unit of its own). Through the cache decorators and the CQRS query cache
(:class:`~pyfly.cache.transaction.TransactionAwareCache`) it does not: their writes wait for the unit's
commit, and what runs at once (reads, ``put_if_absent``, ``evict_if_present``, ``invalidate``) runs outside
the caller's unit (:func:`~pyfly.data.transaction.outside_transaction`), each statement in a short unit of
its own. That costs one more pooled connection of the cache's datasource per cache statement while a
business unit holds its own: size the pool for it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyfly.cache.namespaces import dedicated_cache_name
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
"""The longest stored key (the namespace included) the table takes where the key column needs a length
(MySQL, MariaDB, SQL Server, Oracle)."""

DEFAULT_NAMESPACE = "pyfly:cache:"
"""The key prefix of the cache's entries in the table (as the Redis adapter's)."""


def _glob_tokens(pattern: str) -> list[tuple[str, bool]]:
    """The characters of a glob *pattern*, each with whether it is a wildcard (``*`` or ``?``); a backslash
    makes the next character literal, as in a Redis ``MATCH`` pattern."""
    tokens: list[tuple[str, bool]] = []
    escaped = False
    for ch in pattern:
        if escaped:
            tokens.append((ch, False))
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            tokens.append((ch, ch in ("*", "?")))
    if escaped:
        tokens.append(("\\", False))
    return tokens


def _like_literal(text: str) -> str:
    """*text* matched literally in a LIKE pattern escaped with ``!``."""
    return "".join(_LIKE_ESCAPE + ch if ch in ("%", "_", _LIKE_ESCAPE) else ch for ch in text)


def _glob_to_like(pattern: str) -> str:
    """Translate a glob pattern (``*`` / ``?``; ``\\`` escapes the next character) to a SQL LIKE pattern
    (``%`` / ``_``), escaping with ``!``."""
    result: list[str] = []
    for ch, wildcard in _glob_tokens(pattern):
        if wildcard:
            result.append("%" if ch == "*" else "_")
        else:
            result.append(_like_literal(ch))
    return "".join(result)


def _like_prefix(prefix: str) -> str:
    """A LIKE pattern matching every key that starts with *prefix* (taken literally)."""
    return _like_literal(prefix) + "%"


def _sqlite_glob_literal(text: str) -> str:
    """*text* matched literally in a SQLite ``GLOB`` pattern."""
    return "".join(f"[{ch}]" if ch in ("*", "?", "[") else ch for ch in text)


def _glob_to_sqlite_glob(pattern: str) -> str:
    """Translate a glob pattern (``*`` / ``?``; ``\\`` escapes the next character, every other character is
    literal) to SQLite's ``GLOB``, where ``[`` opens a character class and is matched literally as ``[[]``."""
    return "".join(ch if wildcard else _sqlite_glob_literal(ch) for ch, wildcard in _glob_tokens(pattern))


def _sqlite_glob_prefix(prefix: str) -> str:
    """A SQLite ``GLOB`` pattern matching every key that starts with *prefix* (taken literally)."""
    return _sqlite_glob_literal(prefix) + "*"


class PostgresCacheAdapter:
    """Cache adapter backed by a SQL table (see the module documentation).

    Values are serialized to JSON bytes before storage (identical to the Redis adapter) so any
    JSON-compatible Python object can be cached transparently; a live ORM object, or a value JSON cannot
    represent, raises :class:`~pyfly.cache.serialization.CacheValueError` before anything is written.

    Args:
        engine: where the entries live: an ``AsyncEngine``, a registry ``DataSource`` or a datasource name
            (the adapter does **not** dispose it in :meth:`stop`: it does not own it).
        create_table: create the table at :meth:`start` when it is missing (otherwise only check it).
        purge_interval: how often a write purges expired rows (``None``: only :meth:`purge_expired` does).
        table_name: the table (declared on the framework metadata under that name).
        clock: the current UTC instant (tests pass their own).
        namespace: the key prefix of this cache's entries in the table (``pyfly:cache:``); a ``:`` is
            appended when it does not end with one, so :meth:`clear` never reaches a namespace that merely
            starts with it. An empty namespace declares that the cache owns the whole table: :meth:`clear`
            then empties it, the caches of :meth:`with_namespace` included.
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
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._target = engine
        self._namespace = namespace if not namespace or namespace.endswith(":") else f"{namespace}:"
        self._shared_namespace_reported = False
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
    def namespace(self) -> str:
        """The key prefix of this cache's entries in the table."""
        return self._namespace

    def with_namespace(self, name: str) -> PostgresCacheAdapter:
        """A cache dedicated to *name* on the same table and datasource, disjoint from this one: :meth:`clear`
        never touches it (``pyfly:cache.<name>:`` for the default namespace).

        With an empty namespace this cache owns the whole table, and its :meth:`clear` deletes the dedicated
        cache (``<name>:``) too; the first one made logs a ``cache_not_dedicated`` WARNING. Give the cache a
        namespace to keep idempotency records and orchestration state through a clear. *name* cannot be empty
        or contain ``:`` (:func:`~pyfly.cache.namespaces.dedicated_cache_name`)."""
        dedicated_cache_name(name)
        if not self._namespace and not self._shared_namespace_reported:
            self._shared_namespace_reported = True
            _logger.warning(
                "cache_not_dedicated: this PostgresCacheAdapter has an empty namespace (it owns the whole table), "
                "so its clear() also deletes the dedicated cache %r and every other one; give it a namespace",
                name,
            )
        base = self._namespace[:-1] if self._namespace.endswith(":") else self._namespace
        dedicated = PostgresCacheAdapter(
            self._target,
            create_table=self._create_table,
            purge_interval=timedelta(seconds=self._purge_interval) if self._purge_interval is not None else None,
            table_name=self._table_name,
            clock=self._clock,
            namespace=f"{base}.{name}:" if base else f"{name}:",
        )
        dedicated._started = self._started
        return dedicated

    def _key(self, key: str) -> str:
        """The stored key of *key*: this cache's namespace, then the key."""
        return self._namespace + key

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

    def _check_key(self, stored: str) -> None:
        """Refuse a stored key (namespace included) longer than the key column takes, before any write."""
        if len(stored) > MAX_KEY_LENGTH and self._backend not in _UNBOUNDED_KEY_DIALECTS:
            raise ValueError(
                f"Cache keys are at most {MAX_KEY_LENGTH} characters on {self._backend}, the namespace "
                f"{self._namespace!r} included: {stored[:40]!r}... has {len(stored)}"
            )

    def _key_matches(self, pattern: str, *, prefix: bool = False) -> ColumnElement[bool]:
        """This cache's keys matching glob *pattern*, or starting with *pattern* (taken literally) with *prefix*,
        case-sensitively on every backend: SQLite's ``LIKE`` ignores ASCII case, so SQLite gets its ``GLOB``.
        The namespace is matched literally in front of *pattern*."""
        column = self._table.c.cache_key
        if self._backend == "sqlite":
            namespace = _sqlite_glob_literal(self._namespace)
            if prefix:
                return column.bool_op("GLOB")(namespace + _sqlite_glob_prefix(pattern))
            return column.bool_op("GLOB")(namespace + _glob_to_sqlite_glob(pattern))
        namespace = _like_literal(self._namespace)
        like = _like_prefix(pattern) if prefix else _glob_to_like(pattern)
        return column.like(namespace + like, escape=_LIKE_ESCAPE)

    def _own(self) -> ColumnElement[bool] | None:
        """This cache's rows (``None``: the whole table, for an empty namespace)."""
        return self._key_matches("", prefix=True) if self._namespace else None

    def _live(self, now: datetime) -> ColumnElement[bool]:
        from sqlalchemy import or_

        expires_at = self._table.c.expires_at
        return or_(expires_at.is_(None), expires_at > now)

    @contextlib.asynccontextmanager
    async def _reading(self) -> AsyncIterator[Any]:
        """The session a read runs on: the unit of work bound for the cache's datasource, or a short read unit."""
        async with infrastructure_unit(self._target, read_only=True) as session:
            yield session

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
        """Serialize and upsert *value* with optional TTL."""
        from pyfly.data.relational.upsert import native_upsert, upsert

        await self._ensure_started()
        stored_key = self._key(key)
        self._check_key(stored_key)
        values = {
            "cache_key": stored_key,
            "value": cache_dumps(value),
            "expires_at": self._clock() + ttl if ttl is not None else None,
        }
        async with self._writing(single_statement=native_upsert(self._backend)) as session:
            await upsert(session, self._table, values, key=["cache_key"])
        await self._purge_if_due()

    async def get(self, key: str) -> Any | None:
        """Retrieve and deserialize a cached value, honoring expiry."""
        from sqlalchemy import select

        await self._ensure_started()
        table = self._table
        statement = select(table.c.value).where(table.c.cache_key == self._key(key), self._live(self._clock()))
        async with self._reading() as session:
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
            _logger.warning("Failed to deserialize cached value for key '%s'", key)
            return None

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Store *value* only if *key* is absent or its entry expired; return whether this call stored it.

        One statement on PostgreSQL and SQLite. Elsewhere the expired entry is taken over first, then the
        key inserted if absent, each in a short unit of its own when there is no unit of work to join (two
        callers racing for one key never deadlock on MySQL's gap locks).
        """
        from pyfly.data.relational.upsert import insert_if_absent, native_conditional_insert, take_over

        await self._ensure_started()
        stored_key = self._key(key)
        self._check_key(stored_key)
        now = self._clock()
        table = self._table
        values = {
            "cache_key": stored_key,
            "value": cache_dumps(value),
            "expires_at": now + ttl if ttl is not None else None,
        }
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
            result = await session.execute(delete(table).where(table.c.cache_key == self._key(key)))
        existed = bool(result.rowcount > 0)
        if existed:
            self._evictions += 1
        return existed

    async def evict_by_prefix(self, prefix: str) -> int:
        """Delete every key of this cache starting with *prefix* (taken literally). Returns the number deleted."""
        from sqlalchemy import delete

        await self._ensure_started()
        table = self._table
        statement = delete(table).where(self._key_matches(prefix, prefix=True))
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
        statement = select(literal(1)).where(table.c.cache_key == self._key(key), self._live(self._clock()))
        async with self._reading() as session:
            return (await session.execute(statement)).first() is not None

    async def clear(self) -> None:
        """Delete this cache's entries (its namespace) and nothing else in the table: the caches of
        :meth:`with_namespace`, and whatever else shares the table, survive. With an empty namespace the cache
        owns the table, and this empties it."""
        from sqlalchemy import delete

        await self._ensure_started()
        statement = delete(self._table)
        own = self._own()
        if own is not None:
            statement = statement.where(own)
        async with self._writing(single_statement=True) as session:
            result = await session.execute(statement)
        self._evictions += int(result.rowcount)

    # ------------------------------------------------------------------
    # Expired rows
    # ------------------------------------------------------------------

    async def purge_expired(self) -> int:
        """Delete every expired entry, :attr:`PURGE_BATCH` rows per statement; return how many were deleted."""
        await self._ensure_started()
        purged = 0
        while True:
            deleted = await self._purge_batch()
            purged += deleted
            if deleted < self.PURGE_BATCH:
                return purged

    async def _purge_batch(self) -> int:
        """Delete up to :attr:`PURGE_BATCH` expired entries (all of them where the dialect has no bounded
        ``DELETE``), in one statement; return how many were deleted."""
        from sqlalchemy import delete, select

        table = self._table
        expired = table.c.expires_at <= self._clock()
        if self._backend in ("mysql", "mariadb"):
            # Both spellings: a mariadb:// URL's dialect reads only mariadb_limit, a mysql:// one mysql_limit.
            limit = {"mysql_limit": self.PURGE_BATCH, "mariadb_limit": self.PURGE_BATCH}
            statement: Any = delete(table).where(expired).with_dialect_options(**limit)
        elif self._backend in _UNBOUNDED_KEY_DIALECTS:
            batch = select(table.c.cache_key).where(expired).limit(self.PURGE_BATCH)
            statement = delete(table).where(table.c.cache_key.in_(batch))
        else:
            statement = delete(table).where(expired)
        async with self._writing(single_statement=True) as session:
            return int((await session.execute(statement)).rowcount)

    async def _purge_if_due(self) -> None:
        interval = self._purge_interval
        if interval is None or time.monotonic() - self._last_purge < interval:
            return
        self._last_purge = time.monotonic()
        # After the write's transaction commits (at once when there is none): the purge is not the caller's
        # work and never joins its unit.
        await after_commit(self._purge_quietly, datasource=resolve_manager(self._target).datasource)

    async def _purge_quietly(self) -> None:
        """One batch, so a write never carries a backlog: while a full batch comes back the purge stays due,
        and the next write deletes the next batch."""
        try:
            purged = await self._purge_batch()
        except Exception:  # noqa: BLE001 — a failed purge must never fail a cache write
            _logger.warning("cache_purge_failed", extra={"table": self._table_name}, exc_info=True)
            return
        if purged >= self.PURGE_BATCH:
            self._last_purge = float("-inf")
        if purged:
            _logger.debug("cache_expired_entries_purged", extra={"table": self._table_name, "count": purged})

    # ------------------------------------------------------------------
    # Extended operations (beyond the Protocol minimum)
    # ------------------------------------------------------------------

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Return up to *limit* of this cache's non-expired keys matching the glob *pattern* (``\\`` escapes a
        wildcard), without the namespace."""
        from sqlalchemy import select

        await self._ensure_started()
        table = self._table
        statement = select(table.c.cache_key).where(self._key_matches(pattern), self._live(self._clock())).limit(limit)
        async with self._reading() as session:
            keys = (await session.execute(statement)).scalars().all()
        return [str(key)[len(self._namespace) :] for key in keys]

    async def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate."""
        from sqlalchemy import func, select

        await self._ensure_started()
        own = self._own()
        statement = select(func.count()).select_from(self._table).where(self._live(self._clock()))
        if own is not None:
            statement = statement.where(own)
        async with self._reading() as session:
            size = int((await session.execute(statement)).scalar() or 0)

        requests = self._hits + self._misses
        return {
            "size": size,
            "type": "postgres",
            "namespace": self._namespace,
            "requests": requests,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / requests) if requests else 0.0,
        }
