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
"""Snapshot store SPI + in-memory adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class Snapshot:
    aggregate_id: str
    aggregate_type: str
    sequence: int
    payload: dict[str, Any]


@runtime_checkable
class SnapshotStore(Protocol):
    async def save(self, snapshot: Snapshot) -> None: ...
    async def load(self, aggregate_id: str) -> Snapshot | None: ...
    async def delete(self, aggregate_id: str) -> bool: ...


class InMemorySnapshotStore:
    def __init__(self) -> None:
        self._store: dict[str, Snapshot] = {}
        self._lock = asyncio.Lock()

    async def save(self, snapshot: Snapshot) -> None:
        async with self._lock:
            existing = self._store.get(snapshot.aggregate_id)
            if existing is None or existing.sequence < snapshot.sequence:
                self._store[snapshot.aggregate_id] = snapshot

    async def load(self, aggregate_id: str) -> Snapshot | None:
        async with self._lock:
            return self._store.get(aggregate_id)

    async def delete(self, aggregate_id: str) -> bool:
        async with self._lock:
            return self._store.pop(aggregate_id, None) is not None
