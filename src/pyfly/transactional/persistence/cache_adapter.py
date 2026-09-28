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
"""Cache-backed persistence adapter (InMemoryCache / Redis / Postgres — via pyfly.cache).

Useful when the operator already has a unified cache layer and doesn't want a
second persistence dependency for short-lived orchestration state.

Durability guarantee: all enumeration (find_all / find_stale / cleanup) is
driven by the cache backend's own key-space via the optional ``get_keys``
helper that all PyFly cache adapters expose.  This avoids the anti-pattern of
maintaining a separate in-process index that would be lost on restart.

The provider follows the transaction-aware cache contract
(:class:`~pyfly.cache.transaction.TransactionAwareCache`), as the SQL provider joins the caller's unit of work:
a state saved or deleted inside a unit of work is written once the unit commits, and dropped when it rolls
back; outside one it is written at once. Reads always run at once (a state saved in the current unit is not
seen before the commit). The cache holds the state's JSON text, never a live object: a live ORM entity a
step returned is stored as its text, not referenced.

Concurrent writes are ordered differently than by the SQL provider, whose write outside the unit waits for the
row lock of a unit that wrote the same execution: here it lands at once, and the unit's deferred write lands
after it, at the commit. The engines never write one execution's state from two tasks at once (a background
workflow run started inside a unit starts after that unit commits,
:class:`~pyfly.transactional.workflow.engine.WorkflowEngine`), so this shows only when an application writes
an execution's state itself, inside a unit and outside it at the same time.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.cache.namespaces import dedicated_cache
from pyfly.cache.ports.outbound import CacheAdapter
from pyfly.cache.transaction import TransactionAwareCache
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus
from pyfly.transactional.core.persistence import (
    ExecutionState,
    StateSerializer,
)

ORCHESTRATION_CACHE = "orchestration"
"""The name of the cache dedicated to orchestration state."""


class CachePersistenceProvider:
    """Wraps any PyFly :class:`~pyfly.cache.ports.outbound.CacheAdapter`.

    Durability notes
    ----------------
    * The state lives in the cache dedicated to ``orchestration``
      (:func:`~pyfly.cache.namespaces.dedicated_cache`), not among the
      adapter's own entries, so clearing the application cache never drops
      in-flight executions.
    * Keys are stored under ``<prefix><correlation_id>`` using
      :meth:`~pyfly.cache.ports.outbound.CacheAdapter.put` /
      :meth:`~pyfly.cache.ports.outbound.CacheAdapter.get` /
      :meth:`~pyfly.cache.ports.outbound.CacheAdapter.evict` — the correct
      port methods.
    * ``find_all`` / ``find_stale`` / ``cleanup`` enumerate the live key-space
      via the backend's ``get_keys`` helper (not a local in-process index) so
      they survive process restarts.

    ``get_keys`` is an *optional* extra not present on the base
    :class:`CacheAdapter` Protocol.  All PyFly built-in adapters expose it:

    * :class:`~pyfly.cache.adapters.memory.InMemoryCache` — synchronous,
      no pattern argument; we filter by prefix manually.
    * :class:`~pyfly.cache.adapters.redis.RedisCacheAdapter` — async,
      accepts a glob ``pattern`` argument.
    * PostgresCacheAdapter — async, accepts a glob ``pattern`` argument.

    The implementation detects which flavour is present at runtime.
    """

    def __init__(self, cache_adapter: CacheAdapter, *, key_prefix: str = "orchestration:") -> None:
        # Orchestration state is durable: it lives in the cache dedicated to it, which clearing the
        # application cache (an admin "evict all", a query-cache reset) never touches. Its writes wait for the
        # commit of the unit of work they are made in (see the module documentation).
        self._store = dedicated_cache(cache_adapter, ORCHESTRATION_CACHE)
        self._cache = TransactionAwareCache(self._store)
        self._prefix = key_prefix

    def _key(self, correlation_id: str) -> str:
        return f"{self._prefix}{correlation_id}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_all_keys(self) -> list[str]:
        """Return all cache keys that belong to this provider's prefix."""
        get_keys: Any = getattr(self._store, "get_keys", None)
        if get_keys is None:
            # Fallback: evict_by_prefix knows the prefix; we can't enumerate.
            # This should not happen with any standard PyFly adapter.
            return []

        # RedisCacheAdapter / PostgresCacheAdapter: async, accepts pattern
        if inspect.iscoroutinefunction(get_keys):
            # Try calling with a pattern argument (glob); fall back to no args
            try:
                keys: list[str] = await get_keys(f"{self._prefix}*", 10_000)
            except TypeError:
                keys = await get_keys()
            return [k for k in keys if k.startswith(self._prefix)]

        # InMemoryCache: synchronous, no pattern argument
        keys_sync: list[str] = get_keys()
        return [k for k in keys_sync if k.startswith(self._prefix)]

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def save(self, state: ExecutionState) -> None:
        raw = StateSerializer.serialize(state)
        await self._cache.put(self._key(state.correlation_id), raw)

    async def find(self, correlation_id: str) -> ExecutionState | None:
        raw: Any = await self._cache.get(self._key(correlation_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return StateSerializer.deserialize(str(raw))

    async def find_all(
        self,
        *,
        status: ExecutionStatus | None = None,
        pattern: ExecutionPattern | None = None,
    ) -> list[ExecutionState]:
        keys = await self._get_all_keys()
        results: list[ExecutionState] = []
        for key in keys:
            raw: Any = await self._cache.get(key)
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            state = StateSerializer.deserialize(str(raw))
            if status is not None and state.status != status:
                continue
            if pattern is not None and state.pattern != pattern:
                continue
            results.append(state)
        return results

    async def find_stale(self, before: datetime) -> list[ExecutionState]:
        all_states = await self.find_all()
        return [s for s in all_states if not s.status.is_terminal and s.updated_at < before]

    async def delete(self, correlation_id: str) -> bool:
        """Delete the state; inside a unit of work it is deleted when the unit commits, and ``False`` is
        returned then."""
        return await self._cache.evict(self._key(correlation_id))

    async def cleanup(self, older_than: timedelta) -> int:
        cutoff = datetime.now(UTC) - older_than
        count = 0
        for s in await self.find_all():
            if (
                s.status.is_terminal
                and (s.completed_at or s.updated_at) < cutoff
                and await self.delete(s.correlation_id)
            ):
                count += 1
        return count

    async def is_healthy(self) -> bool:
        try:
            await self._cache.exists("__pyfly_health_check__")
            return True
        except Exception:  # noqa: BLE001
            return False
