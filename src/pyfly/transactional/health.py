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
"""Actuator-style health indicator for the orchestration engine."""

from __future__ import annotations

from typing import Any

from pyfly.transactional.core.persistence import ExecutionPersistenceProvider


class OrchestrationHealthIndicator:
    """Reports orchestration backend health to ``/actuator/health``."""

    def __init__(self, persistence: ExecutionPersistenceProvider) -> None:
        self._persistence = persistence

    async def check(self) -> dict[str, Any]:
        healthy = await self._persistence.is_healthy()
        return {
            "status": "UP" if healthy else "DOWN",
            "details": {"persistence": "ok" if healthy else "unreachable"},
        }
