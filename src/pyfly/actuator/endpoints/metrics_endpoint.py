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
"""Metrics actuator endpoint — Spring Boot ``/actuator/metrics`` JSON parity.

Presents Prometheus registry data the way Spring Boot / Micrometer does:

* ``GET /actuator/metrics`` -> ``{"names": ["http.server.requests", ...]}`` using
  Micrometer DOT-case meter names (the Prometheus exposition keeps underscores).
* ``GET /actuator/metrics/{name}`` -> ``{name, baseUnit, measurements:[{statistic,
  value}], availableTags:[{tag, values}]}`` with the Micrometer ``Statistic`` enum
  (COUNT / TOTAL_TIME / MAX / VALUE), and ``?tag=key:value`` drill-down filtering.
"""

from __future__ import annotations

from typing import Any

try:
    from prometheus_client import REGISTRY
except ImportError:  # pragma: no cover
    REGISTRY = None  # type: ignore[assignment]

# Trailing unit tokens stripped from a Prometheus family name to recover the
# Micrometer meter name + base unit (e.g. http_server_requests_seconds ->
# meter "http.server.requests", baseUnit "seconds").
_UNIT_SUFFIXES = ("seconds", "bytes", "ratio", "celsius", "volts", "joules", "percent", "info")

# Prometheus label names that are structural, not Micrometer tags.
_NON_TAG_LABELS = {"le", "quantile"}


def _meter_name_and_unit(family_name: str) -> tuple[str, str | None]:
    """Recover ``(micrometer_meter_name, base_unit)`` from a Prometheus family name."""
    name = family_name
    unit: str | None = None
    for suffix in _UNIT_SUFFIXES:
        if name.endswith("_" + suffix):
            unit = suffix
            name = name[: -(len(suffix) + 1)]
            break
    return name.replace("_", "."), unit


class MetricsEndpoint:
    """``/actuator/metrics`` with Micrometer-shaped JSON and tag drill-down."""

    supports_selector = True

    @property
    def endpoint_id(self) -> str:
        return "metrics"

    @property
    def enabled(self) -> bool:
        return True

    async def handle(self, context: Any = None) -> dict[str, Any] | None:
        selector = None
        query: dict[str, Any] = {}
        if isinstance(context, dict):
            selector = context.get("selector") or context.get("name")
            query = context.get("query") or {}
        if selector:
            return self._detail(str(selector), query)
        return self._list()

    # -- list -------------------------------------------------------------
    def _list(self) -> dict[str, Any]:
        return {"names": sorted(self._collect_meters().keys())}

    # -- detail -----------------------------------------------------------
    def _detail(self, meter: str, query: dict[str, Any]) -> dict[str, Any] | None:
        meters = self._collect_meters()
        info = meters.get(meter)
        if info is None:
            return None

        tag_filter = self._parse_tag(query)

        counts: dict[str, float] = {}
        max_value: float | None = None
        available: dict[str, set[str]] = {}

        for family, statistic in info["families"]:
            for sample in family.samples:
                if sample.name.endswith("_created"):
                    continue
                labels = sample.labels
                if tag_filter and labels.get(tag_filter[0]) != tag_filter[1]:
                    continue

                for key, value in labels.items():
                    if key not in _NON_TAG_LABELS:
                        available.setdefault(key, set()).add(value)

                stat = self._statistic_for(sample, family, statistic)
                if stat is None:
                    continue
                if stat == "MAX":
                    max_value = sample.value if max_value is None else max(max_value, sample.value)
                else:
                    counts[stat] = counts.get(stat, 0.0) + sample.value

        measurements = [{"statistic": stat, "value": value} for stat, value in counts.items()]
        if max_value is not None:
            measurements.append({"statistic": "MAX", "value": max_value})

        result: dict[str, Any] = {
            "name": meter,
            "measurements": measurements,
            "availableTags": [{"tag": key, "values": sorted(values)} for key, values in sorted(available.items())],
        }
        if info["unit"]:
            result["baseUnit"] = info["unit"]
        return result

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _parse_tag(query: dict[str, Any]) -> tuple[str, str] | None:
        raw = query.get("tag")
        if not raw:
            return None
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        if not raw or ":" not in str(raw):
            return None
        key, _, value = str(raw).partition(":")
        return key, value

    @staticmethod
    def _statistic_for(sample: Any, family: Any, statistic: str | None) -> str | None:
        """Map a Prometheus sample to a Micrometer ``Statistic`` enum value."""
        if statistic == "MAX":
            return "MAX"
        name = sample.name
        if name.endswith("_count"):
            return "COUNT"
        if name.endswith("_sum"):
            return "TOTAL_TIME"
        if name.endswith("_bucket"):
            return None  # histogram buckets are not Micrometer measurements
        ftype = getattr(family, "type", "")
        if ftype == "counter":
            return "COUNT"
        if ftype == "gauge":
            return "VALUE"
        return "VALUE"

    def _collect_meters(self) -> dict[str, dict[str, Any]]:
        """Group Prometheus families by Micrometer meter name.

        A companion ``*_max`` gauge is folded into its base meter as the MAX
        statistic (matching Micrometer's single-meter timer exposition).
        """
        meters: dict[str, dict[str, Any]] = {}
        if REGISTRY is None:
            return meters
        for family in REGISTRY.collect():
            fname = family.name
            statistic: str | None = None
            base = fname
            if fname.endswith("_max"):
                base = fname[: -len("_max")]
                statistic = "MAX"
            meter, unit = _meter_name_and_unit(base)
            entry = meters.setdefault(meter, {"families": [], "unit": unit})
            entry["families"].append((family, statistic))
            if unit and not entry["unit"]:
                entry["unit"] = unit
        return meters
