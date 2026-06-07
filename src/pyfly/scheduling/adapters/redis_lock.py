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
"""Redis-backed :class:`~pyfly.scheduling.lock.DistributedLock` adapter.

Hexagonal: the async Redis client is **injected** by the composition root (the scheduling
auto-config); this module never imports ``redis``. Acquire is an atomic ``SET NX PX``; release
is an owner-token compare-and-delete (so an instance only releases a lock it still owns, never
one that already expired and was re-acquired elsewhere).
"""

from __future__ import annotations

import uuid
from typing import Any

# Release only if we still own the key (GET == our token), atomically.
_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisDistributedLock:
    """Distributed lock over an injected async Redis client."""

    def __init__(self, client: Any, *, key_prefix: str = "pyfly:schedlock:") -> None:
        self._client = client
        self._prefix = key_prefix
        self._token = uuid.uuid4().hex  # per-instance owner token

    def _key(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def try_acquire(self, name: str, ttl: float) -> bool:
        # max(1, ...) guards a sub-millisecond ttl yielding PX 0, which Redis rejects.
        acquired = await self._client.set(self._key(name), self._token, nx=True, px=max(1, int(ttl * 1000)))
        return bool(acquired)

    async def release(self, name: str) -> None:
        await self._client.eval(_RELEASE_LUA, 1, self._key(name), self._token)
