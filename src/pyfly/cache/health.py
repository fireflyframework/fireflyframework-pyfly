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
"""Health indicator for the cache subsystem (audit #74)."""

from __future__ import annotations

import time
from typing import Any

from pyfly.actuator.health import HealthStatus

_PROBE_KEY = "__pyfly_cache_health_probe__"


class CacheHealthIndicator:
    """Probes the cache with a put/get/evict round-trip and reports latency."""

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    async def health(self) -> HealthStatus:
        adapter_name = type(self._adapter).__name__
        try:
            started = time.perf_counter()
            await self._adapter.put(_PROBE_KEY, "ok")
            value = await self._adapter.get(_PROBE_KEY)
            await self._adapter.evict(_PROBE_KEY)
            latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                status="DOWN",
                details={"adapter": adapter_name, "error": type(exc).__name__, "message": str(exc)[:200]},
            )

        if value != "ok":
            return HealthStatus(status="DOWN", details={"adapter": adapter_name, "error": "probe-mismatch"})

        status = "UP" if latency_ms < 1000.0 else "OUT_OF_SERVICE"
        return HealthStatus(status=status, details={"adapter": adapter_name, "latencyMs": round(latency_ms, 2)})
