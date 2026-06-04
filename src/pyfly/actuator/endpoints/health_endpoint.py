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
"""Health actuator endpoint."""

from __future__ import annotations

from typing import Any

from pyfly.actuator.health import DOWN_STATUSES, HealthAggregator, HealthResult


class HealthEndpoint:
    """Exposes aggregated health check results at ``/actuator/health``.

    ``show_details`` / ``show_components`` mirror Spring's
    ``management.endpoint.health.show-details`` / ``show-components`` and control
    how much of each contributor's payload is exposed.
    """

    def __init__(
        self,
        health_aggregator: HealthAggregator,
        *,
        show_details: bool = True,
        show_components: bool = True,
    ) -> None:
        self._aggregator = health_aggregator
        self._show_details = show_details
        self._show_components = show_components

    @property
    def endpoint_id(self) -> str:
        return "health"

    @property
    def enabled(self) -> bool:
        return True

    def _serialize(self, result: HealthResult) -> dict[str, Any]:
        return result.to_dict(show_details=self._show_details, show_components=self._show_components)

    @staticmethod
    def _status_code(status: str) -> int:
        return 503 if status in DOWN_STATUSES else 200

    async def handle(self, context: Any = None) -> dict[str, Any]:
        return self._serialize(await self._aggregator.check())

    async def get_status_code(self) -> int:
        """Return the HTTP status code based on health state."""
        return self._status_code((await self._aggregator.check()).status)

    async def handle_liveness(self) -> dict[str, Any]:
        return self._serialize(await self._aggregator.check_liveness())

    async def handle_readiness(self) -> dict[str, Any]:
        return self._serialize(await self._aggregator.check_readiness())

    async def get_liveness_status_code(self) -> int:
        return self._status_code((await self._aggregator.check_liveness()).status)

    async def get_readiness_status_code(self) -> int:
        return self._status_code((await self._aggregator.check_readiness()).status)

    async def handle_path(self, path: str) -> tuple[dict[str, Any] | None, int]:
        """Drill into ``/actuator/health/{path}`` — a configured group or a single
        component contributor. Returns ``(payload, status_code)`` or ``(None, 404)``
        when *path* matches neither."""
        group = await self._aggregator.check_group(path)
        if group is not None:
            return self._serialize(group), self._status_code(group.status)

        result = await self._aggregator.check()
        component = result.components.get(path)
        if component is not None:
            payload: dict[str, Any] = {"status": component.status}
            if self._show_details and component.details:
                payload["details"] = component.details
            return payload, self._status_code(component.status)

        return None, 404
