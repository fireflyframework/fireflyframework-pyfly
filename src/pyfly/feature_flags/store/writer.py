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
"""``FlagStoreWriter``: every runtime write (actuator, admin, CLI) goes through it.

It validates the definition, writes it to the store (row + change, one transaction), refreshes the writing
process's composition after commit, then publishes ``FeatureFlagUpdated``. Other processes see the change on their next
store poll (spec 4.6). A failing listener is logged; the committed write stands.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pyfly.feature_flags.definitions import parse_document
from pyfly.feature_flags.events import FeatureFlagUpdated
from pyfly.feature_flags.sources.store import STORE_SOURCE
from pyfly.feature_flags.store.ports import CommitAwareFlagStore

if TYPE_CHECKING:
    from pyfly.context.events import ApplicationEventPublisher
    from pyfly.feature_flags.registry import FlagRegistry
    from pyfly.feature_flags.store.ports import FlagChange, FlagStore

__all__ = ["FlagStoreWriter"]

_logger = logging.getLogger(__name__)


class FlagStoreWriter:
    """Writes to the store and keeps the writing process's flags current (see the module documentation)."""

    def __init__(
        self, store: FlagStore, registry: FlagRegistry, *, publisher: ApplicationEventPublisher | None = None
    ) -> None:
        self._store = store
        self._registry = registry
        self._publisher = publisher

    @property
    def store(self) -> FlagStore:
        return self._store

    async def put(
        self, key: str, definition: Mapping[str, Any], *, actor: str | None, expected_version: int | None = None
    ) -> FlagChange:
        canonical = parse_document({"flags": {key: definition}}).flags[key]
        change = await self._store.put(key, canonical, actor=actor, expected_version=expected_version)
        await self._schedule(change)
        return change

    async def delete(self, key: str, *, actor: str | None, expected_version: int | None = None) -> FlagChange:
        change = await self._store.delete(key, actor=actor, expected_version=expected_version)
        await self._schedule(change)
        return change

    async def _schedule(self, change: FlagChange) -> None:
        snapshot = copy.deepcopy(change)
        if isinstance(self._store, CommitAwareFlagStore):
            await self._store.after_commit(lambda: self._after(snapshot))
        else:
            await self._after(snapshot)

    async def _after(self, change: FlagChange) -> None:
        if self._registry.has_source(STORE_SOURCE):
            try:
                await self._registry.refresh(STORE_SOURCE)
                status = next(source for source in self._registry.sources() if source.name == STORE_SOURCE)
                if status.error is not None:
                    _logger.warning(
                        "feature_flag_store_refresh_failed", extra={"flag": change.key, "error": status.error}
                    )
            except Exception:  # noqa: BLE001 — the committed write and its event still stand
                _logger.warning("feature_flag_store_refresh_failed", extra={"flag": change.key}, exc_info=True)
        if self._publisher is None:
            return
        event = FeatureFlagUpdated(change.key, change.action, change.actor, change.previous, change.definition)
        try:
            await self._publisher.publish(event)
        except Exception:  # noqa: BLE001 — the write is committed; a listener never undoes it
            _logger.warning(
                "feature_flag_event_listener_failed",
                extra={"event": "FeatureFlagUpdated", "flag": change.key},
                exc_info=True,
            )
