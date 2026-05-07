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
"""Cache-backed persistence adapter (Caffeine / Redis / Hazelcast — via pyfly.cache).

Useful when the operator already has a unified cache layer and doesn't want a
second persistence dependency for short-lived orchestration state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.transactional.core.persistence import (
    ExecutionState,
)


class CachePersistenceProvider:
    """Wraps any pyfly cache adapter (must support async ``get``/``set``/``delete``)."""

    def __init__(self, cache_adapter: Any, *, key_prefix: str = "orchestration:") -> None:
        self._cache = cache_adapter
        self._prefix = key_prefix
        self._index: set[str] = set()

    def _key(self, correlation_id: str) -> str:
        return f"{self._prefix}{correlation_id}"

    async def save(self, state: ExecutionState) -> None:
        await self._cache.set(self._key(state.correlation_id), state)
        self._index.add(state.correlation_id)

    async def find(self, correlation_id: str) -> ExecutionState | None:
        result: ExecutionState | None = await self._cache.get(self._key(correlation_id))
        return result

    async def find_all(self, *, status: Any = None, pattern: Any = None) -> list[ExecutionState]:
        results: list[ExecutionState] = []
        for cid in list(self._index):
            state = await self.find(cid)
            if state is None:
                continue
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
        result = await self._cache.delete(self._key(correlation_id))
        self._index.discard(correlation_id)
        return bool(result)

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
        return True
