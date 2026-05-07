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
"""Redis-backed :class:`ExecutionPersistenceProvider`.

Stores each :class:`ExecutionState` as a JSON blob under
``<prefix><correlation_id>``.  An optional TTL prevents unbounded growth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pyfly.transactional.core.persistence import (
    ExecutionState,
    StateSerializer,
)


class RedisPersistenceProvider:
    """Adapter that delegates to an injected ``redis.asyncio.Redis`` client.

    The client is expected to expose async methods ``get``, ``set``, ``delete``,
    ``scan_iter`` and ``ping`` (the redis-py asyncio client does).
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = "orchestration:",
        ttl: timedelta | None = None,
    ) -> None:
        self._redis = redis_client
        self._prefix = key_prefix
        self._ttl = ttl

    def _key(self, correlation_id: str) -> str:
        return f"{self._prefix}{correlation_id}"

    async def save(self, state: ExecutionState) -> None:
        raw = StateSerializer.serialize(state)
        if self._ttl is not None:
            await self._redis.set(self._key(state.correlation_id), raw, ex=int(self._ttl.total_seconds()))
        else:
            await self._redis.set(self._key(state.correlation_id), raw)

    async def find(self, correlation_id: str) -> ExecutionState | None:
        raw = await self._redis.get(self._key(correlation_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return StateSerializer.deserialize(raw)

    async def find_all(
        self,
        *,
        status: Any = None,
        pattern: Any = None,
    ) -> list[ExecutionState]:
        results: list[ExecutionState] = []
        async for key in self._redis.scan_iter(f"{self._prefix}*"):
            raw = await self._redis.get(key)
            if raw is None:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            state = StateSerializer.deserialize(raw)
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
        deleted = await self._redis.delete(self._key(correlation_id))
        return bool(deleted)

    async def cleanup(self, older_than: timedelta) -> int:
        cutoff = datetime.now(UTC) - older_than
        all_states = await self.find_all()
        count = 0
        for s in all_states:
            if (
                s.status.is_terminal
                and (s.completed_at or s.updated_at) < cutoff
                and await self.delete(s.correlation_id)
            ):
                count += 1
        return count

    async def is_healthy(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:  # noqa: BLE001
            return False


# Make sure the adapter satisfies the structural Protocol.
