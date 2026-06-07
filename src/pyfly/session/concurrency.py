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
"""Session concurrency control — Spring Security's ``maximumSessions``.

Limits the number of concurrent sessions per authenticated principal. When the cap is
exceeded a new login is either rejected (``reject-new``) or the oldest session is evicted
(``evict-oldest``). Enforced at the single point where a principal becomes bound to a
session (OAuth2 login). With no cap configured the registry is unused and behavior is
unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class SessionRegistry(Protocol):
    """Per-principal index of live session ids (kept separate from the SessionStore)."""

    async def register(self, principal: str, session_id: str, created_at: float) -> None: ...

    async def deregister(self, principal: str, session_id: str) -> None: ...

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        """``(session_id, created_at)`` for *principal*, oldest first."""
        ...

    async def count(self, principal: str) -> int: ...


class InMemorySessionRegistry:
    """In-process :class:`SessionRegistry` (mirrors InMemorySessionStore)."""

    def __init__(self) -> None:
        self._by_principal: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()

    async def register(self, principal: str, session_id: str, created_at: float) -> None:
        async with self._lock:
            self._by_principal.setdefault(principal, {})[session_id] = created_at

    async def deregister(self, principal: str, session_id: str) -> None:
        async with self._lock:
            sessions = self._by_principal.get(principal)
            if sessions is not None:
                sessions.pop(session_id, None)
                if not sessions:
                    del self._by_principal[principal]

    async def list_sessions(self, principal: str) -> list[tuple[str, float]]:
        async with self._lock:
            sessions = self._by_principal.get(principal, {})
            return sorted(sessions.items(), key=lambda kv: kv[1])

    async def count(self, principal: str) -> int:
        async with self._lock:
            return len(self._by_principal.get(principal, {}))


@dataclass(frozen=True)
class ConcurrencyControlPolicy:
    """Concurrency cap configuration."""

    max_sessions: int = -1  # -1 = unlimited (default; behavior unchanged)
    strategy: str = "evict-oldest"  # "evict-oldest" | "reject-new"


class SessionConcurrencyController:
    """Enforces a per-principal session cap on login and cleans up on logout."""

    def __init__(
        self,
        registry: SessionRegistry,
        policy: ConcurrencyControlPolicy,
        *,
        session_deleter: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._delete = session_deleter  # evicts the session-store entry of an evicted session

    async def on_login(self, principal: str, session_id: str, created_at: float) -> bool:
        """Register the new session, enforcing the cap. Returns ``False`` if rejected."""
        if self._policy.max_sessions < 0:
            await self._registry.register(principal, session_id, created_at)
            return True

        existing = [s for s in await self._registry.list_sessions(principal) if s[0] != session_id]
        if len(existing) + 1 <= self._policy.max_sessions:
            await self._registry.register(principal, session_id, created_at)
            return True

        if self._policy.strategy == "reject-new":
            logger.info(
                "Rejected login for %r: max concurrent sessions (%d) reached", principal, self._policy.max_sessions
            )
            return False

        # evict-oldest: drop the oldest sessions until the new one fits under the cap.
        for session_id_to_evict, _created in existing[: len(existing) + 1 - self._policy.max_sessions]:
            if self._delete is not None:
                await self._delete(session_id_to_evict)
            await self._registry.deregister(principal, session_id_to_evict)
        await self._registry.register(principal, session_id, created_at)
        return True

    async def on_logout(self, principal: str, session_id: str) -> None:
        await self._registry.deregister(principal, session_id)
