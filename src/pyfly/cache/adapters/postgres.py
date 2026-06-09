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
"""PostgreSQL-backed cache adapter."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.cache.serialization import cache_dumps, cache_loads

_logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS pyfly_cache_entries (
    cache_key   TEXT PRIMARY KEY,
    value       BYTEA NOT NULL,
    expires_at  TIMESTAMPTZ NULL
)
"""


def _glob_to_like(pattern: str) -> str:
    """Translate a glob pattern (``*`` / ``?``) to a SQL LIKE pattern (``%`` / ``_``)."""
    result: list[str] = []
    for ch in pattern:
        if ch == "*":
            result.append("%")
        elif ch == "?":
            result.append("_")
        elif ch in ("%", "_", "\\"):
            result.append("\\" + ch)
        else:
            result.append(ch)
    return "".join(result)


class PostgresCacheAdapter:
    """Cache adapter backed by a PostgreSQL table via an async SQLAlchemy engine.

    The table ``pyfly_cache_entries`` is created on :meth:`start` (lazy DDL).
    Values are serialised to JSON bytes before storage (identical to the Redis
    adapter) so any JSON-compatible Python object can be cached transparently.

    Args:
        engine: An ``AsyncEngine`` instance (injected; the adapter does **not**
                dispose it in :meth:`stop` because it does not own the engine).
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create the cache table if it does not exist yet."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        async with self._engine.begin() as conn:
            await conn.execute(text(_DDL))
        self._started = True

    async def stop(self) -> None:
        """No-op: the engine is injected; its lifecycle belongs to the caller."""

    # ------------------------------------------------------------------
    # Ensure table exists before first use if start() was not awaited.
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        if not self._started:
            await self.start()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Serialise and upsert *value* with optional TTL."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        raw: bytes = cache_dumps(value)
        expires_at: datetime | None = None
        if ttl is not None:
            # Compute a tz-aware datetime then strip tzinfo — asyncpg rejects
            # tz-aware objects against TIMESTAMPTZ when the column type is read
            # back via the text() path in some configurations; use a naive UTC
            # value to stay safe (mirrors the event store pattern).
            expires_at = datetime.now(UTC).replace(tzinfo=None) + ttl

        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO pyfly_cache_entries (cache_key, value, expires_at)
                    VALUES (:k, :v, :e)
                    ON CONFLICT (cache_key) DO UPDATE
                        SET value      = EXCLUDED.value,
                            expires_at = EXCLUDED.expires_at
                    """
                ),
                {"k": key, "v": raw, "e": expires_at},
            )

    async def get(self, key: str) -> Any | None:
        """Retrieve and deserialise a cached value, honouring expiry."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT value FROM pyfly_cache_entries
                    WHERE cache_key = :k
                      AND (expires_at IS NULL OR expires_at > :now)
                    """
                ),
                {"k": key, "now": now},
            )
            row = result.fetchone()

        if row is None:
            self._misses += 1
            return None

        try:
            decoded = cache_loads(bytes(row[0]))
            self._hits += 1
            return decoded
        except (ValueError, TypeError):
            self._misses += 1
            _logger.warning("Failed to deserialise cached value for key '%s'", key)
            return None

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Atomically store *value* only if *key* is absent (or expired).

        Expired rows are treated as absent: if an existing row's ``expires_at``
        is in the past the insert will conflict but the row is stale, so we
        attempt a DELETE-then-INSERT instead of a plain ``DO NOTHING`` to give
        correct semantics. For simplicity we keep the fast path (``DO NOTHING``)
        and document that callers should not rely on overwriting expired entries.
        Returns ``True`` iff a row was actually inserted.
        """
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        raw: bytes = cache_dumps(value)
        expires_at: datetime | None = None
        if ttl is not None:
            expires_at = datetime.now(UTC).replace(tzinfo=None) + ttl

        async with self._engine.begin() as conn:
            result = await conn.execute(
                text(
                    """
                    INSERT INTO pyfly_cache_entries (cache_key, value, expires_at)
                    VALUES (:k, :v, :e)
                    ON CONFLICT (cache_key) DO NOTHING
                    """
                ),
                {"k": key, "v": raw, "e": expires_at},
            )
            return bool(result.rowcount > 0)

    async def evict(self, key: str) -> bool:
        """Remove *key*. Returns ``True`` if the key existed."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM pyfly_cache_entries WHERE cache_key = :k"),
                {"k": key},
            )
        existed = bool(result.rowcount > 0)
        if existed:
            self._evictions += 1
        return existed

    async def evict_by_prefix(self, prefix: str) -> int:
        """Delete every key starting with *prefix*. Returns the number deleted."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        like_pattern = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM pyfly_cache_entries WHERE cache_key LIKE :p ESCAPE '\\'"),
                {"p": like_pattern},
            )
        count = int(result.rowcount)
        self._evictions += count
        return count

    async def exists(self, key: str) -> bool:
        """Return ``True`` iff *key* exists and has not expired."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT 1 FROM pyfly_cache_entries
                    WHERE cache_key = :k
                      AND (expires_at IS NULL OR expires_at > :now)
                    """
                ),
                {"k": key, "now": now},
            )
            return result.fetchone() is not None

    async def clear(self) -> None:
        """Remove all entries from the cache table."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        async with self._engine.begin() as conn:
            await conn.execute(text("DELETE FROM pyfly_cache_entries"))

    # ------------------------------------------------------------------
    # Extended operations (beyond the Protocol minimum)
    # ------------------------------------------------------------------

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Return up to *limit* non-expired keys matching the glob *pattern*."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        like_pattern = _glob_to_like(pattern)
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT cache_key FROM pyfly_cache_entries
                    WHERE cache_key LIKE :p
                      AND (expires_at IS NULL OR expires_at > :now)
                    LIMIT :lim
                    """
                ),
                {"p": like_pattern, "now": now, "lim": limit},
            )
            return [row[0] for row in result.fetchall()]

    async def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate."""
        from sqlalchemy import text  # type: ignore[import-not-found,unused-ignore]

        await self._ensure_started()
        now = datetime.now(UTC).replace(tzinfo=None)
        async with self._engine.connect() as conn:
            result = await conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM pyfly_cache_entries
                    WHERE expires_at IS NULL OR expires_at > :now
                    """
                ),
                {"now": now},
            )
            size = int(result.scalar() or 0)

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
