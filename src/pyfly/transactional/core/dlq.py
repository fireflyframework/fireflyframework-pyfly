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
"""Dead-letter queue: capture executions that fail terminally for offline review."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable


@dataclass
class DeadLetterEntry:
    """One captured failed execution / step."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    execution_name: str = ""
    correlation_id: str = ""
    step_id: str | None = None
    error_type: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    retry_count: int = 0
    input: Any = None


@runtime_checkable
class DeadLetterStore(Protocol):
    """SPI for persisting dead-letter entries."""

    async def add(self, entry: DeadLetterEntry) -> None: ...
    async def get(self, entry_id: str) -> DeadLetterEntry | None: ...
    async def list(
        self,
        *,
        execution_name: str | None = None,
        correlation_id: str | None = None,
    ) -> list[DeadLetterEntry]: ...
    async def delete(self, entry_id: str) -> bool: ...
    async def clear(self) -> int: ...


class InMemoryDeadLetterStore:
    """Default DLQ adapter — backed by a dict."""

    def __init__(self) -> None:
        self._store: dict[str, DeadLetterEntry] = {}
        self._lock = asyncio.Lock()

    async def add(self, entry: DeadLetterEntry) -> None:
        async with self._lock:
            self._store[entry.id] = entry

    async def get(self, entry_id: str) -> DeadLetterEntry | None:
        async with self._lock:
            return self._store.get(entry_id)

    async def list(
        self,
        *,
        execution_name: str | None = None,
        correlation_id: str | None = None,
    ) -> list[DeadLetterEntry]:
        async with self._lock:
            entries = list(self._store.values())
        if execution_name is not None:
            entries = [e for e in entries if e.execution_name == execution_name]
        if correlation_id is not None:
            entries = [e for e in entries if e.correlation_id == correlation_id]
        return sorted(entries, key=lambda e: e.timestamp, reverse=True)

    async def delete(self, entry_id: str) -> bool:
        async with self._lock:
            return self._store.pop(entry_id, None) is not None

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._store)
            self._store.clear()
            return count


class DeadLetterService:
    """High-level facade that orchestration components call into."""

    def __init__(self, store: DeadLetterStore | None = None) -> None:
        self._store: DeadLetterStore = store or InMemoryDeadLetterStore()

    async def capture(
        self,
        *,
        execution_name: str,
        correlation_id: str,
        error: BaseException,
        step_id: str | None = None,
        input: Any = None,
    ) -> DeadLetterEntry:
        entry = DeadLetterEntry(
            execution_name=execution_name,
            correlation_id=correlation_id,
            step_id=step_id,
            error_type=type(error).__name__,
            error_message=str(error),
            input=input,
        )
        await self._store.add(entry)
        return entry

    async def list(
        self,
        *,
        execution_name: str | None = None,
        correlation_id: str | None = None,
    ) -> list[DeadLetterEntry]:
        return await self._store.list(execution_name=execution_name, correlation_id=correlation_id)

    async def get(self, entry_id: str) -> DeadLetterEntry | None:
        return await self._store.get(entry_id)

    async def mark_retried(self, entry_id: str) -> bool:
        entry = await self._store.get(entry_id)
        if entry is None:
            return False
        entry.retry_count += 1
        await self._store.add(entry)
        return True

    async def delete(self, entry_id: str) -> bool:
        return await self._store.delete(entry_id)
