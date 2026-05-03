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
"""Cache data provider -- cache listing, stats, and management."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.cache.ports.outbound import CacheAdapter
    from pyfly.context.application_context import ApplicationContext


class CacheProvider:
    """Provides cache information and management operations."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context
        self._adapter: CacheAdapter | None = None

    def _resolve_adapter(self) -> Any | None:
        if self._adapter is not None:
            return self._adapter
        try:
            from pyfly.cache.ports.outbound import CacheAdapter

            for _cls, reg in self._context.container._registrations.items():
                if reg.instance is not None and isinstance(reg.instance, CacheAdapter):
                    self._adapter = reg.instance
                    return self._adapter
        except ImportError:
            pass
        return None

    async def get_caches(self) -> dict[str, Any]:
        adapter = self._resolve_adapter()
        if adapter is None:
            return {"available": False, "type": None, "stats": {}, "keys": []}

        result: dict[str, Any] = {
            "available": True,
            "type": type(adapter).__name__,
        }

        # Stats via duck-typing
        if hasattr(adapter, "get_stats"):
            stats = adapter.get_stats()
            if asyncio.iscoroutine(stats):
                stats = await stats
            result["stats"] = stats
        else:
            result["stats"] = {}

        # Keys via duck-typing
        if hasattr(adapter, "get_keys"):
            keys = adapter.get_keys()
            if asyncio.iscoroutine(keys):
                keys = await keys
            result["keys"] = keys
        else:
            result["keys"] = []

        return result

    async def evict_cache(self, key: str | None = None) -> dict[str, Any]:
        adapter = self._resolve_adapter()
        if adapter is None:
            return {"error": "No cache adapter available"}
        if key:
            result = await adapter.evict(key)
            return {"evicted": result, "key": key}
        else:
            await adapter.clear()
            return {"cleared": True}
