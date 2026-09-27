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
"""Redis-backed cache adapter."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

from pyfly.cache.serialization import cache_dumps, cache_loads

_logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "pyfly:cache:"
"""The key prefix of the cache's entries in the Redis database."""

_SCAN_COUNT = 1000
"""Keys per ``SCAN`` round trip, and per ``DEL`` batch."""


def _glob_escape(text: str) -> str:
    """*text* as a literal in a Redis ``MATCH`` pattern."""
    return "".join(f"\\{ch}" if ch in "*?[]\\" else ch for ch in text)


def _decoded(key: Any) -> str:
    return key.decode() if isinstance(key, bytes) else str(key)


class RedisCacheAdapter:
    """Cache adapter that delegates to a ``redis.asyncio.Redis``-like client.

    Values are JSON-serialized before storage (see :mod:`pyfly.cache.serialization`): a live ORM object,
    or a value JSON cannot represent, raises :class:`~pyfly.cache.serialization.CacheValueError` before
    anything is written.

    The entries live under *namespace* (``pyfly:cache:`` by default) in the Redis database, which the
    cache shares with sessions, locks, the event bus and other applications. Keys passed to and returned
    by the adapter are the cache's own (``product:1`` is stored as ``pyfly:cache:product:1``), and
    :meth:`clear` deletes the namespace only: the cache never runs ``FLUSHDB``. An empty namespace
    declares that the cache owns the whole database; :meth:`clear` then deletes every key in it, the caches
    of :meth:`with_namespace` included (a WARNING says so when the first one is made).

    A namespace always ends with ``:`` (``myapp`` becomes ``myapp:``): otherwise :meth:`clear` would also
    delete the keys of any namespace that merely starts with it (``myapp2``, or the ``myapp.idempotency``
    cache of :meth:`with_namespace`).

    Args:
        client: The Redis client.
        namespace: The key prefix of this cache's entries.
    """

    def __init__(self, client: Any, *, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._client = client
        self._namespace = namespace if not namespace or namespace.endswith(":") else f"{namespace}:"
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._available = True
        self._owns_client = True
        self._shared_namespace_reported = False

    @property
    def namespace(self) -> str:
        """The key prefix of this cache's entries."""
        return self._namespace

    def _key(self, key: str) -> str:
        return self._namespace + key

    async def get(self, key: str) -> Any | None:
        """Retrieve and deserialize a cached value."""
        raw = await self._client.get(self._key(key))
        if raw is None:
            self._misses += 1
            return None
        try:
            value = cache_loads(raw)
            self._hits += 1
            return value
        except (ValueError, TypeError):
            self._misses += 1
            _logger.warning("Failed to deserialize cached value for key '%s'", key)
            return None

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        """Serialize and store a value with optional TTL."""
        raw = cache_dumps(value)
        ex = int(ttl.total_seconds()) if ttl is not None else None
        await self._client.set(self._key(key), raw, ex=ex)

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Atomically store *value* only if *key* is absent (audit #75)."""
        ex = int(ttl.total_seconds()) if ttl is not None else None
        stored = await self._client.set(self._key(key), cache_dumps(value), ex=ex, nx=True)
        return bool(stored)

    async def evict(self, key: str) -> bool:
        """Remove a key. Returns True if the key existed."""
        count = await self._client.delete(self._key(key))
        if count:
            self._evictions += 1
        return cast(bool, count > 0)

    async def evict_by_prefix(self, prefix: str) -> int:
        """Evict every key starting with *prefix* (audit #78); glob characters in it are literal.

        One ``SCAN`` walk of the database (``COUNT`` 1000) with a batched ``DEL`` per round trip.
        """
        removed = await self._delete_matching(_glob_escape(self._key(prefix)) + "*")
        self._evictions += removed
        return removed

    async def _delete_matching(self, pattern: str) -> int:
        removed = 0
        batch: list[Any] = []
        async for key in self._client.scan_iter(match=pattern, count=_SCAN_COUNT):
            batch.append(key)
            if len(batch) >= _SCAN_COUNT:
                removed += int(await self._client.delete(*batch))
                batch = []
        if batch:
            removed += int(await self._client.delete(*batch))
        return removed

    async def exists(self, key: str) -> bool:
        """Check whether a key exists."""
        count = await self._client.exists(self._key(key))
        return cast(bool, count > 0)

    async def get_stats(self) -> dict[str, Any]:
        """Return cache statistics including hit-rate (audit #76).

        ``size`` is the size of the whole Redis database (``DBSIZE``), which the cache may share.
        """
        dbsize = await self._client.dbsize()
        requests = self._hits + self._misses
        return {
            "size": dbsize,
            "type": "redis",
            "namespace": self._namespace,
            "requests": requests,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": (self._hits / requests) if requests else 0.0,
        }

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Return up to *limit* of this cache's keys matching the glob *pattern*, via SCAN."""
        keys: list[str] = []
        match = _glob_escape(self._namespace) + pattern
        async for key in self._client.scan_iter(match=match, count=max(limit, _SCAN_COUNT)):
            keys.append(_decoded(key)[len(self._namespace) :])
            if len(keys) >= limit:
                break
        return keys

    async def clear(self) -> None:
        """Delete this cache's entries (its namespace) and nothing else in the database.

        Never ``FLUSHDB``: sessions, locks and other data sharing the database, and the caches of
        :meth:`with_namespace`, survive.
        """
        removed = await self._delete_matching(_glob_escape(self._namespace) + "*")
        self._evictions += removed

    def with_namespace(self, name: str) -> RedisCacheAdapter:
        """A cache dedicated to *name* on the same client, disjoint from this one: :meth:`clear` never
        touches it (``pyfly:cache.<name>:`` for the default namespace).

        With an empty namespace this cache owns the whole database, and its :meth:`clear` deletes the
        dedicated cache (``<name>:``) too; the first one made logs a ``cache_not_dedicated`` WARNING. Give
        the cache a namespace to keep idempotency records and orchestration state through a clear."""
        if not self._namespace and not self._shared_namespace_reported:
            self._shared_namespace_reported = True
            _logger.warning(
                "cache_not_dedicated: this RedisCacheAdapter has an empty namespace (it owns the whole database), "
                "so its clear() also deletes the dedicated cache %r and every other one; give it a namespace",
                name,
            )
        base = self._namespace[:-1] if self._namespace.endswith(":") else self._namespace
        dedicated = RedisCacheAdapter(self._client, namespace=f"{base}.{name}:" if base else f"{name}:")
        dedicated._owns_client = False  # the client stays this adapter's to close
        return dedicated

    async def start(self) -> None:
        """Ping Redis, but degrade gracefully if it is unreachable (audit #79).

        A cold/absent Redis must not abort the whole application startup; the
        adapter marks itself unavailable and operations fail soft instead.
        """
        try:
            await self._client.ping()
            self._available = True
        except Exception as exc:  # noqa: BLE001
            self._available = False
            _logger.warning("Redis cache unavailable at startup; degrading: %s", exc)

    async def is_available(self) -> bool:
        try:
            await self._client.ping()
            self._available = True
        except Exception:  # noqa: BLE001
            self._available = False
        return self._available

    async def stop(self) -> None:
        """Close the underlying Redis connection (a :meth:`with_namespace` cache leaves it to its source)."""
        if self._owns_client:
            await self._client.aclose()
