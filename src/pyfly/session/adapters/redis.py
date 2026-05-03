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
"""Redis-backed session store."""

from __future__ import annotations

import json
import logging
from typing import Any, cast

_logger = logging.getLogger(__name__)

_KEY_PREFIX = "pyfly:session:"


class RedisSessionStore:
    """Session store backed by ``redis.asyncio``.

    Values are JSON-serialized before storage.
    Keys are prefixed with ``pyfly:session:`` for namespace isolation.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}{session_id}"

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve and deserialize session data."""
        raw = await self._client.get(self._key(session_id))
        if raw is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Failed to deserialize session '%s'", session_id)
            return None

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        """Serialize and store session data with a TTL in seconds."""
        raw = json.dumps(data)
        await self._client.set(self._key(session_id), raw.encode(), ex=ttl)

    async def delete(self, session_id: str) -> None:
        """Remove a session."""
        await self._client.delete(self._key(session_id))

    async def exists(self, session_id: str) -> bool:
        """Check whether a session exists."""
        count = await self._client.exists(self._key(session_id))
        return cast(bool, count > 0)
