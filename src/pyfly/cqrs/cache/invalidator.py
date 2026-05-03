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
"""Event-driven cache invalidation for CQRS queries.

Mirrors Java's ``EventDrivenCacheInvalidator`` — when domain events
arrive, matching cache entries are evicted based on registered rules.
"""

from __future__ import annotations

import logging
from typing import Any

from pyfly.cqrs.cache.adapter import QueryCacheAdapter

_logger = logging.getLogger(__name__)


class EventDrivenCacheInvalidator:
    """Evicts CQRS cache entries in response to domain events.

    Register invalidation rules via :meth:`register` then call
    :meth:`on_event` from your event listener.
    """

    def __init__(self, cache: QueryCacheAdapter) -> None:
        self._cache = cache
        self._rules: dict[type, list[str]] = {}

    def register(self, event_type: type, cache_key_pattern: str) -> None:
        """Register a cache key pattern to evict when *event_type* occurs."""
        self._rules.setdefault(event_type, []).append(cache_key_pattern)

    async def on_event(self, event: Any) -> None:
        """Called when a domain event arrives.  Evicts matching cache keys."""
        patterns = self._rules.get(type(event), [])
        for pattern in patterns:
            cache_key = self._resolve_pattern(pattern, event)
            evicted = await self._cache.evict(cache_key)
            if evicted:
                _logger.debug("Cache evicted for key '%s' on event %s", cache_key, type(event).__name__)

    @staticmethod
    def _resolve_pattern(pattern: str, event: Any) -> str:
        """Resolve ``{field}`` placeholders in the pattern from event attributes."""
        import re

        def _replace(match: re.Match[str]) -> str:
            field_name = match.group(1)
            value = getattr(event, field_name, None)
            if value is None:
                _logger.warning(
                    "Cache invalidation pattern field '%s' not found on event %s",
                    field_name,
                    type(event).__name__,
                )
                return match.group(0)
            return str(value)

        return re.sub(r"\{(\w+)\}", _replace, pattern)
