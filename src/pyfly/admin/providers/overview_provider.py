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
"""Overview data provider -- aggregated application summary."""

from __future__ import annotations

import platform
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthAggregator
    from pyfly.context.application_context import ApplicationContext


_start_time = time.monotonic()


class OverviewProvider:
    """Provides aggregated overview data for the admin dashboard."""

    def __init__(
        self,
        context: ApplicationContext,
        health_aggregator: HealthAggregator | None = None,
    ) -> None:
        self._context = context
        self._health_aggregator = health_aggregator

    async def get_overview(self) -> dict[str, Any]:
        # App info
        app_info = {
            "name": self._context.config.get("pyfly.app.name", "PyFly Application"),
            "version": self._context.config.get("pyfly.app.version", "0.1.0"),
            "description": self._context.config.get("pyfly.app.description", ""),
            "profiles": self._context.environment.active_profiles,
            "uptime_seconds": round(time.monotonic() - _start_time, 1),
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "web_port": int(self._context.config.get("pyfly.web.port", 8080)),
        }

        # Framework version
        try:
            from pyfly import __version__

            app_info["framework_version"] = __version__
        except (ImportError, AttributeError):
            app_info["framework_version"] = "0.1.0"

        # Health
        health_data: dict[str, Any] = {"status": "UNKNOWN", "components": {}}
        if self._health_aggregator is not None:
            result = await self._health_aggregator.check()
            health_data = result.to_dict()

        # Bean stats
        stereotypes = self._context.get_bean_counts_by_stereotype()
        bean_data = {
            "total": self._context.bean_count,
            "stereotypes": stereotypes,
        }

        # Wiring counts
        wiring = self._context.wiring_counts

        return {
            "app": app_info,
            "health": health_data,
            "beans": bean_data,
            "wiring": wiring,
        }
