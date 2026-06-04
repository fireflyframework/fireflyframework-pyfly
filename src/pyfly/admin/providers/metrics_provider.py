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
"""Metrics data provider — backed by the Prometheus registry.

Exposes the SAME metric names served at ``/actuator/prometheus`` (Micrometer /
Prometheus names like ``http_server_requests_seconds_count`` and
``process_uptime_seconds``) — not a parallel naming scheme — so the admin Metrics
view is consistent with the scrape endpoint and Spring Boot tooling.
"""

from __future__ import annotations

from typing import Any

# Runtime/infrastructure meter prefixes (vs. application meters).
_INFRA_PREFIXES = ("process_", "system_", "python_", "jvm_")


def _registry_names() -> list[str]:
    """All Prometheus sample names (excluding ``_created`` timestamp series)."""
    try:
        from prometheus_client import REGISTRY
    except ImportError:
        return []
    return sorted(
        {
            sample.name
            for metric in REGISTRY.collect()
            for sample in metric.samples
            if not sample.name.endswith("_created")
        }
    )


class MetricsProvider:
    """Provides metric names + measurements sourced from the Prometheus registry."""

    async def get_metric_names(self) -> dict[str, Any]:
        names = _registry_names()
        infra = [n for n in names if n.startswith(_INFRA_PREFIXES)]
        return {
            "names": names,
            "available": True,
            "has_prometheus": bool(names) or _registry_available(),
            # "Built-in" = runtime/infra meters; the rest are application meters.
            "builtin_count": len(infra),
            "prometheus_count": len(names) - len(infra),
        }

    async def get_metric_detail(self, name: str) -> dict[str, Any]:
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            return {"name": name, "measurements": [], "available": False}

        measurements: list[dict[str, Any]] = []
        description = ""
        unit = ""
        for metric_family in REGISTRY.collect():
            for sample in metric_family.samples:
                if sample.name.endswith("_created"):
                    continue
                if sample.name == name or sample.name.startswith(name + "_"):
                    measurements.append(
                        {
                            "statistic": sample.name.removeprefix(name).lstrip("_") or "value",
                            "value": sample.value,
                            "tags": dict(sample.labels),
                        }
                    )
                    if not description and metric_family.documentation:
                        description = metric_family.documentation
                    if not unit:
                        unit = getattr(metric_family, "unit", None) or ""
        return {
            "name": name,
            "description": description,
            "unit": unit,
            "source": "prometheus",
            "measurements": measurements,
        }


def _registry_available() -> bool:
    try:
        import prometheus_client  # noqa: F401

        return True
    except ImportError:
        return False
