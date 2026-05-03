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
"""Environment data provider — config properties and active profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class EnvProvider:
    """Provides environment and configuration data."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_env(self) -> dict[str, Any]:
        profiles = self._context.environment.active_profiles
        sources = getattr(self._context.config, "loaded_sources", [])

        # Flatten config into dot-notation key-value pairs
        properties = self._flatten(self._context.config.to_dict())

        return {
            "active_profiles": profiles,
            "properties": properties,
            "sources": list(sources),
        }

    def _flatten(self, data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in data.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                result.update(self._flatten(value, full_key))
            else:
                result[full_key] = value
        return result
