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
"""Configuration properties provider — sorted, grouped, masked, source-attributed.

Presents the *effective* configuration (placeholders resolved, env overrides
applied) grouped by prefix and sorted, with secrets masked and each property
attributed to the source it came from — the Spring Boot Admin / ``configprops``
experience the user asked for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyfly.admin.providers import _config_support as cs

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext


class ConfigProvider:
    """Provides configuration properties grouped by prefix, sorted and masked."""

    def __init__(self, context: ApplicationContext) -> None:
        self._context = context

    async def get_config(self) -> dict[str, Any]:
        config = self._context.config

        flat = cs.flatten(cs.effective_dict(config))
        sources = cs.property_sources(config)
        origins = cs.effective_origins(config, list(flat), sources)

        groups: dict[str, dict[str, Any]] = {}
        for full_key in sorted(flat):
            prefix = cs.group_prefix(full_key)
            sub_key = full_key[len(prefix) + 1 :] if full_key.startswith(prefix + ".") else full_key
            groups.setdefault(prefix, {})[sub_key] = {
                "value": cs.mask(config, full_key, flat[full_key]),
                "origin": origins.get(full_key, ""),
                "sensitive": cs.is_sensitive(config, full_key),
            }

        # Emit groups in sorted prefix order.
        ordered = {prefix: groups[prefix] for prefix in sorted(groups)}

        return {
            "groups": ordered,
            "groupCount": len(ordered),
            "propertyCount": len(flat),
            "sourceCount": len(sources),
        }
