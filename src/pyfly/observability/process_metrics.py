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
"""Process & system meters with Micrometer/Spring Boot names.

Spring Boot auto-instruments a JVM with ``process.uptime``, ``process.cpu.usage``,
``system.cpu.usage``, ``system.cpu.count`` (etc.). pyfly emits the closest stdlib
equivalents under the SAME Prometheus names so Spring Boot dashboards/alerts work:

    process_uptime_seconds, process_start_time_seconds,
    process_cpu_usage, system_cpu_usage (when psutil is present),
    system_cpu_count, process_files_open, process_files_max
"""

from __future__ import annotations

import contextlib
import os
import time

try:
    from prometheus_client import REGISTRY
    from prometheus_client.core import GaugeMetricFamily

    _HAS_PROMETHEUS = True
except ImportError:  # pragma: no cover
    _HAS_PROMETHEUS = False

# Approximate process start (import time). Micrometer uses the real OS start time;
# psutil refines this when available.
_START_EPOCH = time.time()
_START_MONOTONIC = time.monotonic()

try:
    import psutil  # type: ignore[import-untyped]

    _PROC = psutil.Process()
    _START_EPOCH = _PROC.create_time()
    _HAS_PSUTIL = True
except Exception:  # noqa: BLE001 - psutil is optional
    _PROC = None
    _HAS_PSUTIL = False


class ProcessMetricsCollector:
    """A prometheus_client collector emitting Micrometer-named process/system gauges."""

    def __init__(self) -> None:
        self._last_wall = time.monotonic()
        self._last_cpu = self._process_cpu_seconds()

    def _process_cpu_seconds(self) -> float:
        times = os.times()
        return times.user + times.system

    def _process_cpu_usage(self) -> float:
        now = time.monotonic()
        cpu = self._process_cpu_seconds()
        wall_delta = now - self._last_wall
        cpu_delta = cpu - self._last_cpu
        self._last_wall = now
        self._last_cpu = cpu
        cores = os.cpu_count() or 1
        if wall_delta <= 0:
            return 0.0
        return max(0.0, min(1.0, cpu_delta / (wall_delta * cores)))

    def collect(self):  # type: ignore[no-untyped-def]
        if not _HAS_PROMETHEUS:
            return

        now = time.time()
        uptime = max(0.0, now - _START_EPOCH)

        yield GaugeMetricFamily("process_uptime_seconds", "The uptime of the process", value=uptime)
        yield GaugeMetricFamily(
            "process_start_time_seconds", "Start time of the process since unix epoch", value=_START_EPOCH
        )
        yield GaugeMetricFamily(
            "system_cpu_count", "The number of processors available to the process", value=float(os.cpu_count() or 1)
        )
        yield GaugeMetricFamily(
            "process_cpu_usage", "The recent CPU usage for the process", value=self._process_cpu_usage()
        )

        if _HAS_PSUTIL and _PROC is not None:
            system_cpu: float | None = None
            with contextlib.suppress(Exception):
                system_cpu = psutil.cpu_percent() / 100.0
            if system_cpu is not None:
                yield GaugeMetricFamily("system_cpu_usage", "The recent CPU usage of the system", value=system_cpu)

            open_fds: int | None = None
            with contextlib.suppress(Exception):
                open_fds = _PROC.num_fds()
            if open_fds is not None:
                yield GaugeMetricFamily("process_files_open", "The open file descriptor count", value=float(open_fds))

        max_fds = self._max_fds()
        if max_fds is not None:
            yield GaugeMetricFamily("process_files_max", "The maximum file descriptor count", value=float(max_fds))

    @staticmethod
    def _max_fds() -> int | None:
        try:
            import resource

            soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            return soft
        except (ImportError, ValueError, OSError):
            return None


_registered = False


def register_process_metrics() -> None:
    """Register the process/system collector into the default registry (idempotent)."""
    global _registered
    if _registered or not _HAS_PROMETHEUS:
        return
    try:
        REGISTRY.register(ProcessMetricsCollector())
        _registered = True
    except ValueError:
        # Already registered (e.g. duplicate timeseries) — treat as success.
        _registered = True
