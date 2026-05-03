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
"""Configuration properties provider — grouped config view."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class ConfigProvider:
    """Provides configuration properties grouped by prefix."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_config(self) -> dict[str, Any]:
        raw = self._context.config.to_dict()
        groups: dict[str, Any] = {}

        for top_key, top_value in raw.items():
            if isinstance(top_value, dict):
                for section_name, section_data in top_value.items():
                    if isinstance(section_data, dict):
                        groups[f"{top_key}.{section_name}"] = section_data
                    else:
                        groups.setdefault(top_key, {})[section_name] = section_data
            else:
                groups.setdefault("root", {})[top_key] = top_value

        return {"groups": groups}
