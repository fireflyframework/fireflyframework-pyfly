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
"""CQRS metrics actuator endpoint.

Mirrors Java's ``CqrsMetricsEndpoint`` — exposes handler counts
and registry information for monitoring.
"""

from __future__ import annotations

from typing import Any

from pyfly.cqrs.command.registry import HandlerRegistry


class CqrsMetricsEndpoint:
    """Provides CQRS-specific metrics for actuator exposure.

    Accessible at ``/actuator/cqrs/metrics`` when the actuator module is active.
    """

    def __init__(self, registry: HandlerRegistry) -> None:
        self._registry = registry

    def get_metrics(self) -> dict[str, Any]:
        return {
            "command_handlers": self._registry.command_handler_count,
            "query_handlers": self._registry.query_handler_count,
            "registered_command_types": sorted(t.__name__ for t in self._registry.get_registered_command_types()),
            "registered_query_types": sorted(t.__name__ for t in self._registry.get_registered_query_types()),
        }
