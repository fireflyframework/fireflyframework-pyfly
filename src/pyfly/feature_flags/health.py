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
"""Health status for configured feature flag sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyfly.actuator.health import HealthStatus
from pyfly.feature_flags.management import iso_instant

if TYPE_CHECKING:
    from pyfly.feature_flags.management import FlagManagement

__all__ = ["FeatureFlagsHealthIndicator"]


class FeatureFlagsHealthIndicator:
    """Report DOWN only when an enabled source has never loaded."""

    def __init__(self, management: FlagManagement) -> None:
        self._management = management

    async def health(self) -> HealthStatus:
        name = self._management.facade.client.provider.get_metadata().name
        registry = self._management.registry
        if registry is None:
            return HealthStatus(status="UP", details={"provider": name})
        sources = registry.sources()
        details = {
            "provider": name,
            "flags": len(registry.composition().flags),
            "expired": registry.expired_keys(),
            "sources": {
                source.name: {
                    "status": source.status,
                    "flags": source.flags,
                    "lastRefresh": iso_instant(source.last_refresh),
                    "error": source.error,
                    "revision": source.revision,
                }
                for source in sources
            },
        }
        down = any(source.enabled and source.status == "DOWN" for source in sources)
        return HealthStatus(status="DOWN" if down else "UP", details=details)
