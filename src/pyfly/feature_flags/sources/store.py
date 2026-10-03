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
"""The ``store`` layer: the writable flags, reloaded when the store's revision (``MAX(change id)``) moves."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.sources import SourceSnapshot
from pyfly.feature_flags.store.ports import CommitAwareFlagStore

if TYPE_CHECKING:
    from pyfly.feature_flags.store.ports import FlagStore

__all__ = ["STORE_SOURCE", "StoreFlagSource"]

STORE_SOURCE = "store"


class StoreFlagSource:
    """Polls the store's revision every ``refresh_interval`` and reloads every row only when it moved.

    The rows are validated as one document (another process, or LaraFly, may have written them); a broken row
    rejects them all and the revision is not remembered, so the next poll validates again. Loads inside this
    store's transaction are deferred; the last committed document stays until a post-commit refresh or poll.
    """

    name = STORE_SOURCE
    fail_fast = False

    def __init__(self, store: FlagStore, *, refresh_interval: float = 5.0) -> None:
        self._store = store
        self.refresh_interval: float | None = refresh_interval
        self._revision: int | None = None

    @property
    def store(self) -> FlagStore:
        return self._store

    async def load(self) -> SourceSnapshot | None:
        if isinstance(self._store, CommitAwareFlagStore) and self._store.transaction_active:
            return None
        revision = await self._store.revision()
        if revision == self._revision:
            return None
        rows = await self._store.all()
        document = parse_document({"flags": {key: row.definition for key, row in rows.items()}})
        self._revision = revision
        return SourceSnapshot(document, revision=str(revision))

    async def close(self) -> None:
        return None
