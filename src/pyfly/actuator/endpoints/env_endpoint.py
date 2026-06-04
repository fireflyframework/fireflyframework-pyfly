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
"""Environment actuator endpoint — Spring Boot ``/actuator/env`` parity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class EnvEndpoint:
    """Exposes profiles + ordered, masked property sources at ``/actuator/env``.

    ``GET /actuator/env``          -> {activeProfiles, propertySources:[{name, properties}]}
    ``GET /actuator/env/{toMatch}`` -> the value of one property across sources.
    """

    supports_selector = True

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    @property
    def endpoint_id(self) -> str:
        return "env"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any] | None:
        selector = None
        if isinstance(context, dict):
            selector = context.get("selector") or context.get("name")
        profiles = list(self._context.environment.active_profiles)
        sources = self._property_sources()

        if selector:
            return self._property_detail(str(selector), profiles, sources)

        return {"activeProfiles": profiles, "propertySources": sources}

    def _property_sources(self) -> list[dict[str, Any]]:
        config = self._context.config
        fn = getattr(config, "property_sources", None)
        if callable(fn):
            result = fn()
            if isinstance(result, list):
                return result
        return []

    def _property_detail(self, name: str, profiles: list[str], sources: list[dict[str, Any]]) -> dict[str, Any]:
        """Spring ``/actuator/env/{toMatch}`` — the property across all sources."""
        winning: dict[str, Any] | None = None
        per_source: list[dict[str, Any]] = []
        for source in sources:
            prop = source.get("properties", {}).get(name)
            if prop is not None:
                per_source.append({"name": source.get("name", ""), "property": prop})
                if winning is None:
                    winning = {"source": source.get("name", ""), "value": prop.get("value")}
        return {
            "property": winning,
            "activeProfiles": profiles,
            "propertySources": per_source,
        }
