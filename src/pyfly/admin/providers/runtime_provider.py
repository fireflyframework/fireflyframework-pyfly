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
"""Runtime data provider — Python process memory, threads, GC, and CPU metrics."""

from __future__ import annotations

import gc
import os
import platform
import threading
import time
from typing import Any


class RuntimeProvider:
    """Provides Python process runtime metrics for the admin dashboard."""

    async def get_runtime(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "memory": self._get_memory(),
            "threads": self._get_threads(),
            "gc": self._get_gc(),
            "cpu": self._get_cpu(),
            "python": {
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
            },
        }

    @staticmethod
    def _get_memory() -> dict[str, Any]:
        result: dict[str, Any] = {"rss_mb": 0.0}
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss_bytes = usage.ru_maxrss
            # macOS reports in bytes, Linux in KB
            rss_mb = rss_bytes / (1024 * 1024) if platform.system() == "Darwin" else rss_bytes / 1024
            result["rss_mb"] = round(rss_mb, 2)
        except ImportError:
            pass
        try:
            import psutil  # type: ignore[import-untyped]

            proc = psutil.Process(os.getpid())
            mem = proc.memory_info()
            result["rss_mb"] = round(mem.rss / (1024 * 1024), 2)
            result["vms_mb"] = round(mem.vms / (1024 * 1024), 2)
        except ImportError:
            pass
        return result

    @staticmethod
    def _get_threads() -> dict[str, Any]:
        return {
            "active": threading.active_count(),
            "names": [t.name for t in threading.enumerate()],
        }

    @staticmethod
    def _get_gc() -> dict[str, Any]:
        stats = gc.get_stats()
        return {
            "collections": [s.get("collections", 0) for s in stats],
            "collected": [s.get("collected", 0) for s in stats],
            "uncollectable": [s.get("uncollectable", 0) for s in stats],
            "enabled": gc.isenabled(),
            "thresholds": list(gc.get_threshold()),
        }

    @staticmethod
    def _get_cpu() -> dict[str, Any]:
        return {
            "process_time_s": round(time.process_time(), 3),
        }
