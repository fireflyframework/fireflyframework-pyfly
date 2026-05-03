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
"""Health data provider -- delegates to HealthAggregator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyfly.actuator.health import HealthAggregator


class HealthProvider:
    """Provides health check data for the admin dashboard."""

    def __init__(self, aggregator: HealthAggregator | None = None) -> None:
        self._aggregator = aggregator

    async def get_health(self) -> dict[str, Any]:
        if self._aggregator is None:
            return {"status": "UNKNOWN", "components": {}}
        result = await self._aggregator.check()
        return result.to_dict()
