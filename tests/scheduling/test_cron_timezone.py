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
"""@scheduled(zone=...) — time-zone-aware cron (v26.06.44)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from pyfly.scheduling.cron import CronExpression
from pyfly.scheduling.decorators import scheduled


def test_cron_without_zone_is_utc() -> None:
    cron = CronExpression("0 0 * * *")  # daily midnight
    after = datetime(2026, 6, 7, 15, 0, tzinfo=UTC)
    nxt = cron.next_fire_time(after)
    assert nxt.hour == 0 and nxt.minute == 0  # UTC midnight


def test_cron_zone_fires_at_zone_midnight() -> None:
    ny = ZoneInfo("America/New_York")
    cron = CronExpression("0 0 * * *", zone="America/New_York")
    after = datetime(2026, 6, 7, 15, 0, tzinfo=ny)  # 3pm in New York
    nxt = cron.next_fire_time(after)
    assert nxt.hour == 0 and nxt.minute == 0  # midnight in New York
    assert str(nxt.tzinfo) == "America/New_York"


def test_zone_changes_the_utc_instant() -> None:
    # The same "midnight" cron resolves to different UTC instants per zone.
    utc_next = CronExpression("0 0 * * *").next_fire_time(datetime(2026, 6, 7, 15, 0, tzinfo=UTC))
    ny_next = CronExpression("0 0 * * *", zone="America/New_York").next_fire_time(
        datetime(2026, 6, 7, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    )
    assert utc_next.astimezone(UTC) != ny_next.astimezone(UTC)


def test_scheduled_decorator_records_zone() -> None:
    @scheduled(cron="0 0 * * *", zone="America/New_York")
    def nightly_job() -> None:
        pass

    assert nightly_job.__pyfly_scheduled_zone__ == "America/New_York"  # type: ignore[attr-defined]
