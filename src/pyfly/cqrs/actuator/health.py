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
"""CQRS health indicator for actuator.

Mirrors Java's ``CqrsHealthIndicator`` — reports UP when at least
one handler is registered.
"""

from __future__ import annotations

from pyfly.actuator.health import HealthStatus
from pyfly.cqrs.command.registry import HandlerRegistry


class CqrsHealthIndicator:
    """Health check for the CQRS subsystem.

    Reports ``UP`` if at least one command or query handler is registered,
    otherwise ``UNKNOWN`` (the subsystem is present but idle).
    """

    def __init__(self, registry: HandlerRegistry) -> None:
        self._registry = registry

    async def health(self) -> HealthStatus:
        commands = self._registry.command_handler_count
        queries = self._registry.query_handler_count
        status = "UP" if (commands + queries) > 0 else "UNKNOWN"
        return HealthStatus(
            status=status,
            details={"command_handlers": commands, "query_handlers": queries},
        )
