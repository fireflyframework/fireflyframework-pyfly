# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Dead-letter queue for the EDA module — capture events that fail processing."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope


@dataclass
class EdaDeadLetterEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event: EventEnvelope = field(default_factory=lambda: EventEnvelope("", {}, ""))
    error_type: str = ""
    error_message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 0


@runtime_checkable
class EdaDeadLetterStore(Protocol):
    async def add(self, entry: EdaDeadLetterEntry) -> None: ...
    async def list(self, *, limit: int = 100) -> list[EdaDeadLetterEntry]: ...
    async def delete(self, entry_id: str) -> bool: ...


class InMemoryEdaDeadLetterStore:
    def __init__(self) -> None:
        self._store: dict[str, EdaDeadLetterEntry] = {}
        self._lock = asyncio.Lock()

    async def add(self, entry: EdaDeadLetterEntry) -> None:
        async with self._lock:
            self._store[entry.id] = entry

    async def list(self, *, limit: int = 100) -> list[EdaDeadLetterEntry]:
        async with self._lock:
            entries = list(self._store.values())
        return sorted(entries, key=lambda e: e.timestamp, reverse=True)[:limit]

    async def delete(self, entry_id: str) -> bool:
        async with self._lock:
            return self._store.pop(entry_id, None) is not None
