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


# Spring Boot status severity (most → least severe). Aggregation reports the
# most-severe status present; HTTP 503 is returned for DOWN / OUT_OF_SERVICE.
_STATUS_SEVERITY = {"DOWN": 4, "OUT_OF_SERVICE": 3, "UP": 2, "UNKNOWN": 1}
DOWN_STATUSES = ("DOWN", "OUT_OF_SERVICE")


def aggregate_status(statuses: list[str]) -> str:
    """Return the most-severe status (Spring ``SimpleStatusAggregator`` order).

    Non-canonical statuses (e.g. legacy ``DEGRADED``) are treated as ``DOWN``."""
    if not statuses:
        return "UP"
    worst = max(statuses, key=lambda s: _STATUS_SEVERITY.get(s, _STATUS_SEVERITY["DOWN"]))
    return worst if worst in _STATUS_SEVERITY else "DOWN"


@dataclass
class HealthStatus:
    """Health status for a single component (UP / DOWN / OUT_OF_SERVICE / UNKNOWN)."""

    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    """Aggregated health result across all components."""

    status: str
    components: dict[str, HealthStatus] = field(default_factory=dict)

    def to_dict(self, *, show_details: bool = True, show_components: bool = True) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary.

        ``show_components``/``show_details`` mirror Spring's
        ``management.endpoint.health.show-components`` / ``show-details``.
        """
        result: dict[str, Any] = {"status": self.status}
        if self.components and show_components:
            components: dict[str, Any] = {}
            for name, hs in self.components.items():
                entry: dict[str, Any] = {"status": hs.status}
                if show_details and hs.details:
                    entry["details"] = hs.details
                components[name] = entry
            result["components"] = components
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
        # Named health groups (Spring ``management.endpoint.health.group.<name>``):
        # group name -> set of indicator names included in that group.
        self._custom_groups: dict[str, set[str]] = {}

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

    def add_group(self, name: str, indicator_names: set[str]) -> None:
        """Register a named health group with the indicator names it includes."""
        self._custom_groups[name] = set(indicator_names)

    async def check_group(self, name: str) -> HealthResult | None:
        """Run a named group's indicators. Returns ``None`` if no such group.

        The built-in ``liveness``/``readiness`` probe groups are always available;
        other groups must be registered via :meth:`add_group`."""
        if name == "liveness":
            return await self.check_liveness()
        if name == "readiness":
            return await self.check_readiness()
        if name in self._custom_groups:
            members = self._custom_groups[name]
            filtered = {n: ind for n, ind in self._indicators.items() if n in members}
            return await self._check_indicators(filtered)
        return None

    async def _check_indicators(self, indicators: dict[str, HealthIndicator]) -> HealthResult:
        if not indicators:
            return HealthResult(status="UP")

        components: dict[str, HealthStatus] = {}

        for name, indicator in indicators.items():
            try:
                components[name] = await indicator.health()
            except Exception:
                logger.exception("Health indicator '%s' raised an exception", name)
                components[name] = HealthStatus(status="DOWN", details={"error": "check failed"})

        overall = aggregate_status([hs.status for hs in components.values()])
        return HealthResult(status=overall, components=components)
