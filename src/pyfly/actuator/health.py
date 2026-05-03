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
"""Health indicator protocol, status dataclasses, aggregator, and probe groups."""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class ProbeGroup(enum.Enum):
    """Kubernetes-style probe groups for health indicators."""

    LIVENESS = "liveness"
    READINESS = "readiness"


@dataclass
class HealthStatus:
    """Health status for a single component."""

    status: str  # "UP", "DOWN", "DEGRADED"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    """Aggregated health result across all components."""

    status: str
    components: dict[str, HealthStatus] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary."""
        result: dict[str, Any] = {"status": self.status}
        if self.components:
            result["components"] = {
                name: {"status": hs.status, "details": hs.details} for name, hs in self.components.items()
            }
        return result


@runtime_checkable
class HealthIndicator(Protocol):
    """Protocol that beans can implement to contribute health information."""

    async def health(self) -> HealthStatus: ...


class HealthAggregator:
    """Collects health indicators and produces an aggregated result."""

    def __init__(self) -> None:
        self._indicators: dict[str, HealthIndicator] = {}
        self._groups: dict[str, set[ProbeGroup]] = {}

    def add_indicator(
        self,
        name: str,
        indicator: HealthIndicator,
        groups: set[ProbeGroup] | None = None,
    ) -> None:
        """Register a named health indicator with optional probe group membership."""
        self._indicators[name] = indicator
        self._groups[name] = groups if groups else set()

    async def check(self) -> HealthResult:
        """Run all indicators and return an aggregated health result.

        Rules:
        - If any indicator reports DOWN, overall status is DOWN.
        - If an indicator raises an exception, it is treated as DOWN.
        - If no indicators are registered, overall status is UP.
        """
        return await self._check_indicators(self._indicators)

    async def check_liveness(self) -> HealthResult:
        """Run only liveness-group indicators and return an aggregated result."""
        filtered = {
            name: ind
            for name, ind in self._indicators.items()
            if not self._groups[name] or ProbeGroup.LIVENESS in self._groups[name]
        }
        return await self._check_indicators(filtered)

    async def check_readiness(self) -> HealthResult:
        """Run only readiness-group indicators and return an aggregated result."""
        filtered = {
            name: ind
            for name, ind in self._indicators.items()
            if not self._groups[name] or ProbeGroup.READINESS in self._groups[name]
        }
        return await self._check_indicators(filtered)

    async def _check_indicators(self, indicators: dict[str, HealthIndicator]) -> HealthResult:
        if not indicators:
            return HealthResult(status="UP")

        components: dict[str, HealthStatus] = {}
        overall = "UP"

        for name, indicator in indicators.items():
            try:
                status = await indicator.health()
                components[name] = status
                if status.status == "DOWN":
                    overall = "DOWN"
            except Exception:
                logger.exception("Health indicator '%s' raised an exception", name)
                components[name] = HealthStatus(status="DOWN", details={"error": "check failed"})
                overall = "DOWN"

        return HealthResult(status=overall, components=components)
