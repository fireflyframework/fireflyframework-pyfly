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
"""Redis-backed :class:`~pyfly.session.concurrency.SessionRegistry` adapter.

Hexagonal: the async Redis client is **injected** by the composition root (the session
concurrency auto-config); this module never imports ``redis``. Each principal's live sessions
are a Redis sorted set (score = ``created_at``, member = ``session_id``), so ``list_sessions``
is naturally oldest-first and the cross-process index is shared by all app instances.
"""

from __future__ import annotations

from typing import Any


class RedisSessionRegistry:
    """Per-principal session index over an injected async Redis client."""

    def __init__(self, client: Any, *, key_prefix: str = "pyfly:session:user:", ttl: int = 86400) -> None:
        self._client = client
        self._prefix = key_prefix
        self._ttl = ttl

    def _key(self, principal: str) -> str:
        return f"{self._prefix}{principal}"

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        key = self._key(principal)
        await self._client.zadd(key, {session_id: created_at})
        await self._client.expire(key, self._ttl)  # bound orphan growth (slides on each login)

    async def deregister(self, principal: str, session_id: str) -> None:
        await self._client.zrem(self._key(principal), session_id)

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        raw = await self._client.zrange(self._key(principal), 0, -1, withscores=True)
        result: list[tuple[str, float]] = []
        for member, score in raw:
            sid = member.decode() if isinstance(member, bytes) else member
            result.append((sid, float(score)))
        return result  # ZRANGE is ascending -> oldest first

    async def count(self, principal: str) -> int:
        return int(await self._client.zcard(self._key(principal)))
