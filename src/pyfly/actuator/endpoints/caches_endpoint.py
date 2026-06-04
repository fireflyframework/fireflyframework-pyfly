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
"""Caches actuator endpoint — Spring Boot ``/actuator/caches`` parity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext

_CACHE_MANAGER = "cacheManager"


class CachesEndpoint:
    """Exposes configured caches at ``/actuator/caches`` (+ ``/{name}`` selector)."""

    supports_selector = True

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "caches"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any] | None:
        selector = None
        if isinstance(context, dict):
            selector = context.get("selector") or context.get("name")

        caches = self._caches()
        if selector:
            entry = caches.get(selector)
            if entry is None:
                return None
            return {"name": selector, "cacheManager": _CACHE_MANAGER, "target": entry["target"]}

        return {"cacheManagers": {_CACHE_MANAGER: {"caches": caches}}}

    def _caches(self) -> dict[str, Any]:
        adapter = self._resolve_adapter()
        if adapter is None:
            return {}
        name = str(self._context.config.get("pyfly.cache.provider", "default"))
        return {name: {"target": type(adapter).__module__ + "." + type(adapter).__qualname__}}

    def _resolve_adapter(self) -> Any | None:
        try:
            from pyfly.cache.ports.outbound import CacheAdapter
        except ImportError:
            return None
        for _cls, reg in self._context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, CacheAdapter):
                return reg.instance
        return None
