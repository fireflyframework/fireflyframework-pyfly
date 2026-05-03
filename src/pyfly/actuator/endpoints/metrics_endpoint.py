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
"""Metrics actuator endpoint — queries Prometheus registry for metric data."""

from __future__ import annotations

from typing import Any

try:
    from prometheus_client import REGISTRY
except ImportError:
    REGISTRY = None  # type: ignore[assignment]


class MetricsEndpoint:
    """Endpoint at ``/actuator/metrics`` — lists all registered metrics.

    Supports drill-down: pass ``context={"name": "http_requests_total"}``
    to get measurements and tags for a specific metric.
    """

    @property
    def endpoint_id(self) -> str:
        return "metrics"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any]:
        if context and isinstance(context, dict) and "name" in context:
            return self._get_metric_detail(context["name"])
        return self._list_metrics()

    def _list_metrics(self) -> dict[str, Any]:
        names: list[str] = sorted({sample.name for metric in REGISTRY.collect() for sample in metric.samples})
        return {"names": names}

    def _get_metric_detail(self, name: str) -> dict[str, Any]:
        measurements: list[dict[str, Any]] = []
        for metric_family in REGISTRY.collect():
            for sample in metric_family.samples:
                if sample.name == name or sample.name.startswith(name + "_"):
                    measurements.append(
                        {
                            "statistic": sample.name.removeprefix(name).lstrip("_") or "value",
                            "value": sample.value,
                            "tags": dict(sample.labels),
                        }
                    )
        return {
            "name": name,
            "measurements": measurements,
        }
