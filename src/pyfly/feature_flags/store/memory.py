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
"""A process-local FlagStore with the same write semantics as the SQL driver."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.store.ports import FlagChange, FlagConflictError, FlagNotStoredError, StoredFlag

__all__ = ["MemoryFlagStore"]


class MemoryFlagStore:
    """A FlagStore whose data lives for the lifetime of this instance."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._rows: dict[str, StoredFlag] = {}
        self._changes: list[FlagChange] = []
        self._clock = clock or (lambda: datetime.now(UTC))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @staticmethod
    def _copy(row: StoredFlag) -> StoredFlag:
        return replace(row, definition=copy.deepcopy(row.definition))

    async def all(self) -> dict[str, StoredFlag]:
        return {key: self._copy(row) for key, row in self._rows.items()}

    async def get(self, key: str) -> StoredFlag | None:
        row = self._rows.get(key)
        return self._copy(row) if row is not None else None

    async def revision(self) -> int:
        return self._changes[-1].id if self._changes else 0

    def _prepare_change(
        self,
        key: str,
        action: str,
        definition: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        actor: str | None,
        now: datetime,
    ) -> tuple[FlagChange, FlagChange]:
        change = FlagChange(len(self._changes) + 1, key, action, definition, previous, actor, now)
        return change, copy.deepcopy(change)

    async def put(
        self, key: str, definition: Mapping[str, Any], *, actor: str | None, expected_version: int | None = None
    ) -> FlagChange:
        current = self._rows.get(key)
        actual = current.version if current is not None else 0
        if expected_version is not None and expected_version != actual:
            raise FlagConflictError(key, expected_version, actual)
        canonical = parse_document({"flags": {key: definition}}).flags[key]
        now = self._clock()
        previous = copy.deepcopy(current.definition) if current is not None else None
        change, result = self._prepare_change(key, "put", copy.deepcopy(canonical), previous, actor, now)
        self._rows[key] = StoredFlag(key, canonical, actual + 1, now, actor)
        self._changes.append(change)
        return result

    async def delete(self, key: str, *, actor: str | None, expected_version: int | None = None) -> FlagChange:
        current = self._rows.get(key)
        if current is None:
            raise FlagNotStoredError(key)
        if expected_version is not None and expected_version != current.version:
            raise FlagConflictError(key, expected_version, current.version)
        now = self._clock()
        change, result = self._prepare_change(key, "delete", None, copy.deepcopy(current.definition), actor, now)
        del self._rows[key]
        self._changes.append(change)
        return result

    async def history(self, key: str, limit: int = 50) -> list[FlagChange]:
        return [copy.deepcopy(change) for change in reversed(self._changes) if change.key == key][:limit]
