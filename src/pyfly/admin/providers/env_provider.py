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
"""Environment data provider — Spring Boot ``/actuator/env`` shaped data.

Returns active profiles, ordered + masked property sources (each property
attributed to its origin), and an effective, sorted, masked flat view for the
properties table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyfly.admin.providers import _config_support as cs

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class EnvProvider:
    """Provides environment and configuration data, Spring ``/actuator/env`` style."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_env(self) -> dict[str, Any]:
        config = self._context.config
        profiles = list(self._context.environment.active_profiles)

        sources = cs.property_sources(config)

        # Effective (resolved + env-overridden), masked, sorted flat view.
        effective = cs.flatten(cs.effective_dict(config))
        properties = {key: cs.mask(config, key, effective[key]) for key in sorted(effective)}
        origins = cs.effective_origins(config, list(properties), sources)

        source_names = [s.get("name", "") for s in sources] or list(getattr(config, "loaded_sources", []))

        return {
            # Spring Boot /actuator/env shape:
            "activeProfiles": profiles,
            "propertySources": sources,
            # Back-compat + table convenience:
            "active_profiles": profiles,
            "properties": properties,
            "origins": origins,
            "sources": source_names,
        }
