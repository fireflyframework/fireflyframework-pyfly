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
"""Named caches: regions of a cache, and caches of their own.

One ``CacheAdapter`` bean serves several consumers (the declarative decorators, the CQRS query cache,
HTTP idempotency records, orchestration state). They must not clear each other's entries:

- A **region** (:func:`cache_region`, :class:`PrefixedCache`) is a named part of a cache: its keys live
  under ``<name>::`` and its :meth:`~PrefixedCache.clear` evicts that prefix only. Clearing the cache it
  belongs to clears the region too, which is right for evictable data (the CQRS query cache is the
  ``:cqrs:`` region of the application cache).
- A **dedicated cache** (:func:`dedicated_cache`) is disjoint from the cache it comes from: clearing that
  cache never touches it. Durable consumers (idempotency records, orchestration state) keep their entries
  there. The built-in adapters implement it with ``with_namespace(name)``: a separate store in memory, a
  separate key namespace on Redis and in the PostgreSQL adapter's table.

No clear ever flushes a store other data may share (``FLUSHDB`` on Redis).
"""

from __future__ import annotations

import fnmatch
import inspect
import logging
from datetime import timedelta
from typing import Any

from pyfly.cache.ports.outbound import CacheAdapter

_logger = logging.getLogger("pyfly.cache")

REGION_SEPARATOR = "::"
"""What separates a region's name from the keys in it."""

NAMESPACE_SEPARATOR = ":"
"""What ends a key namespace (``pyfly:cache:``, ``pyfly:cache.idempotency:``); a dedicated cache's name cannot
hold it."""

_WARNED: set[type] = set()


class PrefixedCache:
    """A named region of *delegate*: every key lives under *prefix*, and :meth:`clear` evicts that prefix
    and nothing else.

    It does not own *delegate*: :meth:`start` and :meth:`stop` leave it alone.
    """

    def __init__(self, delegate: CacheAdapter, prefix: str) -> None:
        if not prefix:
            raise ValueError("A cache region needs a non-empty prefix")
        self._delegate = delegate
        self._prefix = prefix

    @property
    def delegate(self) -> CacheAdapter:
        """The cache this region is part of."""
        return self._delegate

    @property
    def prefix(self) -> str:
        """The prefix of every key in this region."""
        return self._prefix

    async def get(self, key: str) -> Any | None:
        return await self._delegate.get(self._prefix + key)

    async def put(self, key: str, value: Any, ttl: timedelta | None = None) -> None:
        await self._delegate.put(self._prefix + key, value, ttl=ttl)

    async def put_if_absent(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        return await self._delegate.put_if_absent(self._prefix + key, value, ttl=ttl)

    async def evict(self, key: str) -> bool:
        return await self._delegate.evict(self._prefix + key)

    async def evict_by_prefix(self, prefix: str) -> int:
        return await self._delegate.evict_by_prefix(self._prefix + prefix)

    async def exists(self, key: str) -> bool:
        return await self._delegate.exists(self._prefix + key)

    async def clear(self) -> None:
        """Evict every entry of this region (the rest of the cache is left alone)."""
        await self._delegate.evict_by_prefix(self._prefix)

    async def get_keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """Up to *limit* keys of this region matching the glob *pattern*, without the region's prefix."""
        get_keys: Any = getattr(self._delegate, "get_keys", None)
        if get_keys is None:
            return []
        if inspect.iscoroutinefunction(get_keys):
            keys: list[str] = await get_keys(_glob_escape(self._prefix) + pattern, limit)
        else:
            keys = get_keys()
        found: list[str] = []
        for key in keys:
            if key.startswith(self._prefix) and fnmatch.fnmatchcase(key[len(self._prefix) :], pattern):
                found.append(key[len(self._prefix) :])
                if len(found) >= limit:
                    break
        return found

    def with_namespace(self, name: str) -> PrefixedCache:
        """This region inside a cache dedicated to *name* (see :func:`dedicated_cache`)."""
        return PrefixedCache(dedicated_cache(self._delegate, name), self._prefix)

    async def start(self) -> None:
        """Nothing to start: the region does not own its cache."""

    async def stop(self) -> None:
        """Nothing to stop: the region does not own its cache."""


def _glob_escape(text: str) -> str:
    return "".join(f"\\{ch}" if ch in "*?[]\\" else ch for ch in text)


def cache_region(cache: CacheAdapter, name: str) -> PrefixedCache:
    """The region *name* of *cache*: keys under ``<name>::``, cleared with it (see the module docs)."""
    if not name:
        raise ValueError("A cache region needs a name")
    return PrefixedCache(cache, f"{name}{REGION_SEPARATOR}")


def dedicated_cache_name(name: str) -> str:
    """*name*, checked as the name of a dedicated cache (``with_namespace(name)``): not empty, and without
    :data:`NAMESPACE_SEPARATOR`, which ends a namespace. ``with_namespace("a:b")`` would otherwise store its
    entries under ``pyfly:cache.a:b:``, inside the namespace ``with_namespace("a")`` clears (and a key
    ``b:k`` of that cache would be one of its keys). A ``.`` is fine: ``pyfly:cache.a.b:`` is disjoint from
    ``pyfly:cache.a:``. Raises ``ValueError``."""
    if not name:
        raise ValueError("A dedicated cache needs a name")
    if NAMESPACE_SEPARATOR in name:
        raise ValueError(
            f"A dedicated cache's name cannot contain {NAMESPACE_SEPARATOR!r}, which ends a namespace (it would "
            f"be part of another dedicated cache): {name!r}"
        )
    return name


def dedicated_cache(cache: CacheAdapter, name: str) -> CacheAdapter:
    """A cache for *name* that *cache*'s ``clear()`` never touches (*name*: :func:`dedicated_cache_name`).

    Adapters that implement ``with_namespace(name)`` (every built-in one) return a disjoint cache that
    shares their connection. For another adapter this falls back to the region ``pyfly.<name>::`` of
    *cache*, logged once per adapter type: its entries are then cleared with *cache*.
    """
    dedicated_cache_name(name)
    factory: Any = getattr(cache, "with_namespace", None)
    if callable(factory):
        dedicated: CacheAdapter = factory(name)
        return dedicated
    if type(cache) not in _WARNED:
        _WARNED.add(type(cache))
        _logger.warning(
            "cache_not_dedicated: %s has no with_namespace(), so the dedicated cache %r is a region of it and "
            "clear() on it also clears %r",
            type(cache).__qualname__,
            name,
            name,
        )
    return PrefixedCache(cache, f"pyfly.{name}{REGION_SEPARATOR}")
