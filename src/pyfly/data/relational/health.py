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
"""``HealthIndicator`` for SQLAlchemy async engines.

Pings the database with ``SELECT 1`` and reports the dialect on the
``details`` payload so the actuator response makes it obvious what is
being checked.
"""

from __future__ import annotations

from typing import Any

from pyfly.actuator.health import HealthStatus


class SqlAlchemyHealthIndicator:
    """Database health probe — ``UP`` iff ``SELECT 1`` succeeds."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def health(self) -> HealthStatus:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            return HealthStatus(
                status="DOWN",
                details={"error": type(exc).__name__, "message": str(exc)[:200]},
            )
        dialect = getattr(getattr(self._engine, "dialect", None), "name", "unknown")
        return HealthStatus(status="UP", details={"database": dialect})
