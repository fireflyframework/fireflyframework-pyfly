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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from croniter import croniter  # type: ignore[import-untyped]


@dataclass(frozen=True)
class CronExpression:
    """Wraps a cron expression string for next-fire-time calculations."""

    expression: str  # 5-field "min hour dom month dow" or Spring 6-field "sec min hour dom month dow"
    zone: str | None = None  # IANA time zone (e.g. "America/New_York"); None = UTC
    _normalized: str = field(init=False, default="", repr=False, compare=False)
    _seconds_first: bool = field(init=False, default=False, repr=False, compare=False)
    _tz: Any = field(init=False, default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Validate and normalize the cron expression.

        Accepts Spring-style 6-field (seconds-first) cron and the ``?`` day
        placeholder, not just standard 5-field cron (audit #185). When *zone* is set,
        fire times are computed in that IANA time zone (Spring ``@Scheduled(zone=...)``).
        """
        normalized = self.expression.replace("?", "*").strip()
        six_field = len(normalized.split()) == 6
        object.__setattr__(self, "_normalized", normalized)
        object.__setattr__(self, "_seconds_first", six_field)
        object.__setattr__(self, "_tz", ZoneInfo(self.zone) if self.zone else None)
        try:
            croniter(normalized, datetime.now(UTC), second_at_beginning=six_field)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Invalid cron expression: {self.expression}") from exc

    def _now(self) -> datetime:
        """Current time in the configured zone (UTC when no zone is set)."""
        return datetime.now(self._tz) if self._tz is not None else datetime.now(UTC)

    def _cron(self, base: datetime) -> Any:
        return croniter(self._normalized, base, second_at_beginning=self._seconds_first)

    def next_fire_time(self, after: datetime | None = None) -> datetime:
        """Return the next fire time after the given datetime (default: now)."""
        base = after or self._now()
        return cast(datetime, self._cron(base).get_next(datetime))

    def previous_fire_time(self, before: datetime | None = None) -> datetime:
        """Return the previous fire time before the given datetime."""
        base = before or self._now()
        return cast(datetime, self._cron(base).get_prev(datetime))

    def next_n_fire_times(self, n: int, after: datetime | None = None) -> list[datetime]:
        """Return the next N fire times."""
        base = after or self._now()
        cron = self._cron(base)
        return [cron.get_next(datetime) for _ in range(n)]

    def seconds_until_next(self, after: datetime | None = None) -> float:
        """Return seconds until the next fire time."""
        now = after or self._now()
        next_time = self.next_fire_time(now)
        return (next_time - now).total_seconds()
