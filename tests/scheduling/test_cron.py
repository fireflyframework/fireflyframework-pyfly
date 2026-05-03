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
"""Tests for CronExpression wrapper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pyfly.scheduling.cron import CronExpression


class TestCronExpression:
    def test_valid_expression_creates_instance(self) -> None:
        cron = CronExpression("*/5 * * * *")
        assert cron.expression == "*/5 * * * *"

    def test_invalid_expression_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron expression"):
            CronExpression("not a cron")

    def test_next_fire_time_returns_correct_time(self) -> None:
        cron = CronExpression("30 14 * * *")  # Every day at 14:30
        base = datetime(2026, 1, 15, 10, 0, 0)
        result = cron.next_fire_time(after=base)
        assert result == datetime(2026, 1, 15, 14, 30, 0)

    def test_next_fire_time_without_base_defaults_to_now(self) -> None:
        cron = CronExpression("* * * * *")  # Every minute
        now = datetime.now(UTC)
        result = cron.next_fire_time()
        # The next fire time should be within 60 seconds from now
        delta = (result - now).total_seconds()
        assert 0 < delta <= 60

    def test_previous_fire_time_works(self) -> None:
        cron = CronExpression("0 12 * * *")  # Every day at noon
        base = datetime(2026, 1, 15, 14, 0, 0)
        result = cron.previous_fire_time(before=base)
        assert result == datetime(2026, 1, 15, 12, 0, 0)

    def test_next_n_fire_times_returns_correct_count_and_ascending(self) -> None:
        cron = CronExpression("0 * * * *")  # Every hour
        base = datetime(2026, 1, 15, 10, 0, 0)
        times = cron.next_n_fire_times(5, after=base)
        assert len(times) == 5
        # Verify ascending order
        for i in range(1, len(times)):
            assert times[i] > times[i - 1]
        # Verify expected times
        assert times[0] == datetime(2026, 1, 15, 11, 0, 0)
        assert times[4] == datetime(2026, 1, 15, 15, 0, 0)

    def test_seconds_until_next_returns_positive_float(self) -> None:
        cron = CronExpression("* * * * *")  # Every minute
        base = datetime(2026, 1, 15, 10, 0, 0)
        seconds = cron.seconds_until_next(after=base)
        assert isinstance(seconds, float)
        assert seconds > 0

    def test_every_minute_cron_fires_within_60_seconds(self) -> None:
        cron = CronExpression("* * * * *")
        base = datetime(2026, 1, 15, 10, 0, 30)
        seconds = cron.seconds_until_next(after=base)
        assert 0 < seconds <= 60

    def test_midnight_cron_fires_at_midnight(self) -> None:
        cron = CronExpression("0 0 * * *")
        base = datetime(2026, 1, 15, 23, 0, 0)
        result = cron.next_fire_time(after=base)
        assert result == datetime(2026, 1, 16, 0, 0, 0)
