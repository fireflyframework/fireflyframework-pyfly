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
"""Cron expression wrapper for next-fire-time calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from croniter import croniter  # type: ignore[import-untyped]


@dataclass(frozen=True)
class CronExpression:
    """Wraps a cron expression string for next-fire-time calculations."""

    expression: str  # Standard 5-field cron: "minute hour day month weekday"

    def __post_init__(self) -> None:
        """Validate the cron expression."""
        if not croniter.is_valid(self.expression):
            raise ValueError(f"Invalid cron expression: {self.expression}")

    def next_fire_time(self, after: datetime | None = None) -> datetime:
        """Return the next fire time after the given datetime (default: now)."""
        base = after or datetime.now(UTC)
        cron = croniter(self.expression, base)
        return cast(datetime, cron.get_next(datetime))

    def previous_fire_time(self, before: datetime | None = None) -> datetime:
        """Return the previous fire time before the given datetime."""
        base = before or datetime.now(UTC)
        cron = croniter(self.expression, base)
        return cast(datetime, cron.get_prev(datetime))

    def next_n_fire_times(self, n: int, after: datetime | None = None) -> list[datetime]:
        """Return the next N fire times."""
        base = after or datetime.now(UTC)
        cron = croniter(self.expression, base)
        return [cron.get_next(datetime) for _ in range(n)]

    def seconds_until_next(self, after: datetime | None = None) -> float:
        """Return seconds until the next fire time."""
        now = after or datetime.now(UTC)
        next_time = self.next_fire_time(now)
        return (next_time - now).total_seconds()
