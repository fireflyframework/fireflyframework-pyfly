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
"""The FlagStore port: writable definitions, versions and history (spec 4.6)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

__all__ = ["FlagChange", "FlagConflictError", "FlagNotStoredError", "FlagStore", "FlagStoreError", "StoredFlag"]


@dataclass(frozen=True)
class StoredFlag:
    """One stored flagd definition and its latest writer."""

    key: str
    definition: dict[str, Any]
    version: int
    updated_at: datetime
    updated_by: str | None


@dataclass(frozen=True)
class FlagChange:
    """A committed write; ``id`` is the revision it produced."""

    id: int
    key: str
    action: str
    definition: dict[str, Any] | None
    previous: dict[str, Any] | None
    actor: str | None
    changed_at: datetime


class FlagStoreError(RuntimeError):
    """A write the store refused."""


class FlagConflictError(FlagStoreError):
    """The expected version did not match, so nothing changed."""

    def __init__(self, key: str, expected: int | None, actual: int | None) -> None:
        found = "another write in between" if actual is None else f"version {actual}"
        super().__init__(f"feature flag {key!r} was changed concurrently: expected version {expected}, found {found}")
        self.key = key
        self.expected = expected
        self.actual = actual


class FlagNotStoredError(FlagStoreError):
    """A delete targeted a key absent from the store."""

    def __init__(self, key: str) -> None:
        super().__init__(f"the flag store holds no definition of {key!r}")
        self.key = key


@runtime_checkable
class FlagStore(Protocol):
    """Every write updates a row and appends one change atomically."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def all(self) -> dict[str, StoredFlag]: ...

    async def get(self, key: str) -> StoredFlag | None: ...

    async def revision(self) -> int: ...

    async def put(
        self, key: str, definition: Mapping[str, Any], *, actor: str | None, expected_version: int | None = None
    ) -> FlagChange: ...

    async def delete(self, key: str, *, actor: str | None, expected_version: int | None = None) -> FlagChange: ...

    async def history(self, key: str, limit: int = 50) -> list[FlagChange]: ...
