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
"""In-memory session store with TTL-based expiry."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class InMemorySessionStore:
    """In-memory session store with TTL support and asyncio.Lock for safety.

    Suitable for development, testing, and single-process applications.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: str) -> dict[str, Any] | None:
        """Retrieve session data. Returns ``None`` if missing or expired."""
        async with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return None

            data, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[session_id]
                return None

            return data

    async def save(self, session_id: str, data: dict[str, Any], ttl: int) -> None:
        """Store session data with a TTL in seconds."""
        async with self._lock:
            expires_at = time.monotonic() + ttl
            self._store[session_id] = (data, expires_at)

    async def replace(self, session_id: str, data: dict[str, Any], ttl: int) -> bool:
        """Replace the session's data and TTL only if the store holds it and it has not expired (under the
        lock); ``False`` otherwise, and nothing is written."""
        async with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return False
            if time.monotonic() > entry[1]:
                del self._store[session_id]
                return False
            self._store[session_id] = (data, time.monotonic() + ttl)
            return True

    async def rename(self, old_id: str, new_id: str, data: dict[str, Any], ttl: int) -> bool:
        """Move the session from *old_id* to *new_id* with *data* and a new TTL, only if the store holds it
        under *old_id* and it has not expired (under the lock); ``False`` otherwise, and nothing is written."""
        async with self._lock:
            entry = self._store.pop(old_id, None)
            if entry is None or time.monotonic() > entry[1]:
                return False
            self._store[new_id] = (data, time.monotonic() + ttl)
            return True

    async def delete(self, session_id: str) -> None:
        """Remove a session."""
        async with self._lock:
            self._store.pop(session_id, None)

    async def exists(self, session_id: str) -> bool:
        """Check if a session exists and is not expired."""
        async with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return False
            _, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[session_id]
                return False
            return True
