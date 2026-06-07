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
"""Redis-backed OAuth2 :class:`TokenStore` adapter.

Cross-instance refresh-token persistence + fast distributed revocation for a multi-instance
authorization server. Hexagonal: the async Redis client is injected by the composition root;
this module never imports ``redis``. Tokens are stored as JSON strings with an optional TTL
(typically the refresh-token lifetime) so expired tokens self-evict.
"""

from __future__ import annotations

import json
from typing import Any


class RedisTokenStore:
    """OAuth2 token store over an injected async Redis client."""

    def __init__(self, client: Any, *, ttl: int | None = None, key_prefix: str = "pyfly:oauth2:token:") -> None:
        self._client = client
        self._ttl = ttl
        self._prefix = key_prefix

    def _key(self, token_id: str) -> str:
        return f"{self._prefix}{token_id}"

    async def store(self, token_id: str, token_data: dict[str, Any]) -> None:
        payload = json.dumps(token_data)
        if self._ttl:
            await self._client.set(self._key(token_id), payload, ex=self._ttl)
        else:
            await self._client.set(self._key(token_id), payload)

    async def find(self, token_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(self._key(token_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)  # type: ignore[no-any-return]

    async def revoke(self, token_id: str) -> None:
        await self._client.delete(self._key(token_id))
