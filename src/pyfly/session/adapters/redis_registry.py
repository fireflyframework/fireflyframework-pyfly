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
is naturally oldest-first and the cross-process index is shared by all app instances. The capped
registration (:meth:`RedisSessionRegistry.register_limited`) is one Lua script over the set, so concurrent
logins never exceed the cap.
"""

from __future__ import annotations

from typing import Any

from pyfly.session.concurrency import SessionRegistration

# KEYS: the principal's set. ARGV: session_id, created_at, max_sessions, evict_oldest (1/0), ttl.
# Returns {accepted (1/0), evicted session ids...}.
_REGISTER_LIMITED = """
local others = redis.call('ZCARD', KEYS[1])
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  others = others - 1
end
local cap = tonumber(ARGV[3])
local evicted = {}
if others + 1 > cap then
  if ARGV[4] ~= '1' then
    return {0}
  end
  local excess = others + 1 - math.max(cap, 0)
  for _, member in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
    if #evicted >= excess then
      break
    end
    if member ~= ARGV[1] then
      table.insert(evicted, member)
    end
  end
  for _, member in ipairs(evicted) do
    redis.call('ZREM', KEYS[1], member)
  end
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[5])
local result = {1}
for _, member in ipairs(evicted) do
  table.insert(result, member)
end
return result
"""


class RedisSessionRegistry:
    """Per-principal session index over an injected async Redis client."""

    def __init__(self, client: Any, *, key_prefix: str = "pyfly:session:user:", ttl: int = 86400) -> None:
        self._client = client
        self._prefix = key_prefix
        self._ttl = ttl
        self._register_limited: Any = None

    @property
    def key_prefix(self) -> str:
        """The prefix of the principals' keys."""
        return self._prefix

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

    async def register_limited(
        self, principal: str, session_id: str, created_at: float, *, max_sessions: int, evict_oldest: bool
    ) -> SessionRegistration:
        """Check the cap, evict the oldest (with *evict_oldest*) and register, in one Lua script."""
        if self._register_limited is None:
            self._register_limited = self._client.register_script(_REGISTER_LIMITED)
        reply = await self._register_limited(
            keys=[self._key(principal)],
            args=[session_id, created_at, max_sessions, "1" if evict_oldest else "0", self._ttl],
        )
        evicted = tuple(member.decode() if isinstance(member, bytes) else str(member) for member in reply[1:])
        return SessionRegistration(bool(int(reply[0])), evicted)
