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
"""Persistence port + in-memory adapter for orchestration executions.

Mirrors ``org.fireflyframework.orchestration.core.persistence`` —
:class:`ExecutionPersistenceProvider` is the SPI; concrete adapters
(in-memory, Redis, cache, event-sourced) live alongside or in
``pyfly.transactional.persistence``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pyfly.transactional.core.context import ExecutionContext
from pyfly.transactional.core.model import ExecutionPattern, ExecutionStatus


@dataclass
class ExecutionState:
    """Serializable snapshot of an :class:`ExecutionContext`."""

    correlation_id: str
    name: str
    pattern: ExecutionPattern
    status: ExecutionStatus
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    payload: dict[str, Any]

    @classmethod
    def from_context(cls, ctx: ExecutionContext) -> ExecutionState:
        return cls(
            correlation_id=ctx.correlation_id,
            name=ctx.name,
            pattern=ctx.pattern,
            status=ctx.status,
            started_at=ctx.started_at,
            updated_at=ctx.updated_at,
            completed_at=ctx.completed_at,
            payload=ctx.to_dict(),
        )

    def to_context(self) -> ExecutionContext:
        return ExecutionContext.from_dict(self.payload)


class StateSerializer:
    """JSON serializer for :class:`ExecutionState` (Redis / file / network)."""

    @staticmethod
    def serialize(state: ExecutionState) -> str:
        data = {
            "correlation_id": state.correlation_id,
            "name": state.name,
            "pattern": state.pattern.value,
            "status": state.status.value,
            "started_at": state.started_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "completed_at": state.completed_at.isoformat() if state.completed_at else None,
            "payload": state.payload,
        }
        return json.dumps(data, default=str)

    @staticmethod
    def deserialize(raw: str) -> ExecutionState:
        data = json.loads(raw)
        return ExecutionState(
            correlation_id=data["correlation_id"],
            name=data["name"],
            pattern=ExecutionPattern(data["pattern"]),
            status=ExecutionStatus(data["status"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            completed_at=(
                datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None
            ),
            payload=data["payload"],
        )


@runtime_checkable
class ExecutionPersistenceProvider(Protocol):
    """SPI implemented by every persistence backend."""

    async def save(self, state: ExecutionState) -> None: ...
    async def find(self, correlation_id: str) -> ExecutionState | None: ...
    async def find_all(
        self,
        *,
        status: ExecutionStatus | None = None,
        pattern: ExecutionPattern | None = None,
    ) -> list[ExecutionState]: ...
    async def find_stale(self, before: datetime) -> list[ExecutionState]: ...
    async def delete(self, correlation_id: str) -> bool: ...
    async def cleanup(self, older_than: timedelta) -> int: ...
    async def is_healthy(self) -> bool: ...


class InMemoryPersistenceProvider:
    """Thread-safe dict-backed adapter — default when nothing else is configured."""

    def __init__(self) -> None:
        self._store: dict[str, ExecutionState] = {}
        self._lock = asyncio.Lock()

    async def save(self, state: ExecutionState) -> None:
        async with self._lock:
            self._store[state.correlation_id] = state

    async def find(self, correlation_id: str) -> ExecutionState | None:
        async with self._lock:
            return self._store.get(correlation_id)

    async def find_all(
        self,
        *,
        status: ExecutionStatus | None = None,
        pattern: ExecutionPattern | None = None,
    ) -> list[ExecutionState]:
        async with self._lock:
            results = list(self._store.values())
        if status is not None:
            results = [s for s in results if s.status == status]
        if pattern is not None:
            results = [s for s in results if s.pattern == pattern]
        return results

    async def find_stale(self, before: datetime) -> list[ExecutionState]:
        async with self._lock:
            return [
                s
                for s in self._store.values()
                if not s.status.is_terminal and s.updated_at < before
            ]

    async def delete(self, correlation_id: str) -> bool:
        async with self._lock:
            return self._store.pop(correlation_id, None) is not None

    async def cleanup(self, older_than: timedelta) -> int:
        cutoff = datetime.now(UTC) - older_than
        async with self._lock:
            doomed = [
                cid
                for cid, s in self._store.items()
                if s.status.is_terminal and (s.completed_at or s.updated_at) < cutoff
            ]
            for cid in doomed:
                del self._store[cid]
            return len(doomed)

    async def is_healthy(self) -> bool:
        return True
