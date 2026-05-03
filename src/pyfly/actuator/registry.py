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
"""ActuatorRegistry — collects and manages actuator endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.actuator.ports import ActuatorEndpoint

if TYPE_CHECKING:
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config


class ActuatorRegistry:
    """Registry of :class:`ActuatorEndpoint` instances.

    Supports per-endpoint enable/disable via configuration:
    ``pyfly.actuator.endpoints.{endpoint_id}.enabled``
    """

    def __init__(self, config: Config | None = None) -> None:
        self._endpoints: dict[str, ActuatorEndpoint] = {}
        self._config = config

    def register(self, endpoint: ActuatorEndpoint) -> None:
        """Register an actuator endpoint."""
        self._endpoints[endpoint.endpoint_id] = endpoint

    def get_enabled_endpoints(self) -> dict[str, ActuatorEndpoint]:
        """Return all endpoints that are currently enabled.

        Enable state is determined by (highest priority first):
        1. Config key ``pyfly.actuator.endpoints.{id}.enabled``
        2. The endpoint's own ``enabled`` property
        """
        result: dict[str, ActuatorEndpoint] = {}
        for eid, ep in self._endpoints.items():
            if self._is_enabled(ep):
                result[eid] = ep
        return result

    def discover_from_context(self, context: ApplicationContext) -> None:
        """Auto-discover ``ActuatorEndpoint`` beans from the DI container."""
        for _cls, reg in context.container._registrations.items():
            if reg.instance is not None and isinstance(reg.instance, ActuatorEndpoint):
                ep = reg.instance
                if ep.endpoint_id not in self._endpoints:
                    self._endpoints[ep.endpoint_id] = ep

    def _is_enabled(self, endpoint: ActuatorEndpoint) -> bool:
        """Check if an endpoint is enabled considering config overrides."""
        if self._config is not None:
            config_key = f"pyfly.actuator.endpoints.{endpoint.endpoint_id}.enabled"
            override = self._config.get(config_key)
            if override is not None:
                if isinstance(override, bool):
                    return override
                return str(override).lower() in ("true", "1", "yes")
        return endpoint.enabled
