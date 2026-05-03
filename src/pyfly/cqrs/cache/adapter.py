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
"""Query cache adapter — bridges pyfly.cache with CQRS.

Mirrors Java's ``QueryCacheAdapter`` with the `:cqrs:` key prefix.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

_logger = logging.getLogger(__name__)

CQRS_CACHE_PREFIX = ":cqrs:"


class QueryCacheAdapter:
    """Thin wrapper around pyfly's :class:`CacheAdapter` with CQRS-specific key prefixing.

    If no underlying cache is provided, all operations are silent no-ops.
    """

    def __init__(self, cache: Any = None) -> None:
        self._cache = cache

    # ── read ───────────────────────────────────────────────────

    async def get(self, cache_key: str) -> Any | None:
        if self._cache is None:
            return None
        prefixed = f"{CQRS_CACHE_PREFIX}{cache_key}"
        try:
            return await self._cache.get(prefixed)
        except Exception as exc:
            _logger.warning("CQRS cache get failed for key '%s': %s", prefixed, exc)
            return None

    # ── write ──────────────────────────────────────────────────

    async def put(self, cache_key: str, value: Any, ttl: timedelta | None = None) -> None:
        if self._cache is None:
            return
        prefixed = f"{CQRS_CACHE_PREFIX}{cache_key}"
        try:
            await self._cache.put(prefixed, value, ttl=ttl)
        except Exception as exc:
            _logger.warning("CQRS cache put failed for key '%s': %s", prefixed, exc)

    # ── evict ──────────────────────────────────────────────────

    async def evict(self, cache_key: str) -> bool:
        if self._cache is None:
            return False
        prefixed = f"{CQRS_CACHE_PREFIX}{cache_key}"
        try:
            return cast(bool, await self._cache.evict(prefixed))
        except Exception as exc:
            _logger.warning("CQRS cache evict failed for key '%s': %s", prefixed, exc)
            return False

    # ── clear ──────────────────────────────────────────────────

    async def clear(self) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.clear()
        except Exception as exc:
            _logger.warning("CQRS cache clear failed: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._cache is not None
