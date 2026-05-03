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
"""Metrics data provider — built-in process metrics + optional Prometheus."""

from __future__ import annotations

import contextlib
import gc
import os
import sys
import threading
import time
from typing import Any

_BUILTIN_PREFIX = "process."
_PYTHON_PREFIX = "python."

_METRIC_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    # (description, unit)
    "process.cpu_time_user": ("User CPU time", "seconds"),
    "process.cpu_time_system": ("System CPU time", "seconds"),
    "process.memory_rss": ("Resident set size", "bytes"),
    "process.memory_vms": ("Virtual memory size", "bytes"),
    "process.pid": ("Process ID", ""),
    "process.threads": ("Active thread count", ""),
    "process.uptime": ("Process uptime", "seconds"),
    "process.open_fds": ("Open file descriptors", ""),
    "python.gc_gen0_collections": ("GC generation 0 collections", ""),
    "python.gc_gen1_collections": ("GC generation 1 collections", ""),
    "python.gc_gen2_collections": ("GC generation 2 collections", ""),
    "python.gc_objects_tracked": ("Objects tracked by GC", ""),
    "python.gc_objects_uncollectable": ("Uncollectable GC objects", ""),
    "python.version": ("Python version", ""),
    "python.implementation": ("Python implementation", ""),
}

_PROCESS_START = time.monotonic()


def _collect_builtin_metrics() -> list[dict[str, Any]]:
    """Collect built-in process and Python runtime metrics."""
    metrics: list[dict[str, Any]] = []

    def _add(name: str, value: Any) -> None:
        desc, unit = _METRIC_DESCRIPTIONS.get(name, ("", ""))
        metrics.append(
            {
                "name": name,
                "value": value,
                "description": desc,
                "unit": unit,
                "source": "builtin",
            }
        )

    # Process metrics
    try:
        times = os.times()
        _add("process.cpu_time_user", round(times.user, 3))
        _add("process.cpu_time_system", round(times.system, 3))
    except (AttributeError, OSError):
        pass

    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # maxrss is in KB on Linux, bytes on macOS
        rss_bytes = usage.ru_maxrss
        if sys.platform == "darwin":
            pass  # already bytes
        else:
            rss_bytes *= 1024
        _add("process.memory_rss", rss_bytes)
    except (ImportError, AttributeError):
        pass

    _add("process.pid", os.getpid())
    _add("process.threads", threading.active_count())
    _add("process.uptime", round(time.monotonic() - _PROCESS_START, 1))

    with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
        _add("process.open_fds", len(os.listdir(f"/proc/{os.getpid()}/fd")))

    # Python runtime metrics
    gc_stats = gc.get_stats()
    for i, gen in enumerate(gc_stats):
        _add(f"python.gc_gen{i}_collections", gen.get("collections", 0))

    _add("python.gc_objects_tracked", len(gc.get_objects()))
    gc_garbage = gc.garbage
    _add("python.gc_objects_uncollectable", len(gc_garbage))

    _add("python.version", sys.version.split()[0])
    _add("python.implementation", sys.implementation.name)

    return metrics


class MetricsProvider:
    """Provides metrics data — built-in process metrics + optional Prometheus."""

    async def get_metric_names(self) -> dict[str, Any]:
        # Always include built-in metrics
        builtin = _collect_builtin_metrics()
        builtin_names = [m["name"] for m in builtin]

        prometheus_names: list[str] = []
        has_prometheus = False
        try:
            from prometheus_client import REGISTRY

            prometheus_names = sorted({sample.name for metric in REGISTRY.collect() for sample in metric.samples})
            has_prometheus = True
        except ImportError:
            pass

        all_names = sorted(set(builtin_names + prometheus_names))
        return {
            "names": all_names,
            "available": True,
            "has_prometheus": has_prometheus,
            "builtin_count": len(builtin_names),
            "prometheus_count": len(prometheus_names),
        }

    async def get_metric_detail(self, name: str) -> dict[str, Any]:
        # Check built-in metrics first
        if name.startswith(_BUILTIN_PREFIX) or name.startswith(_PYTHON_PREFIX):
            builtin = _collect_builtin_metrics()
            for m in builtin:
                if m["name"] == name:
                    return {
                        "name": name,
                        "description": m["description"],
                        "unit": m["unit"],
                        "source": "builtin",
                        "measurements": [
                            {
                                "statistic": "value",
                                "value": m["value"],
                                "tags": {},
                            }
                        ],
                    }

        # Fall back to Prometheus
        try:
            from prometheus_client import REGISTRY

            measurements: list[dict[str, Any]] = []
            description = ""
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
                        if not description and metric_family.documentation:
                            description = metric_family.documentation
            return {
                "name": name,
                "description": description,
                "unit": "",
                "source": "prometheus",
                "measurements": measurements,
            }
        except ImportError:
            return {"name": name, "measurements": [], "available": False}
