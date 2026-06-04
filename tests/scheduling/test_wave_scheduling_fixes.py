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
"""Regression tests for scheduling/resilience fixes (#184, #185)."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from pyfly.kernel.exceptions import OperationTimeoutException
from pyfly.resilience.time_limiter import time_limiter
from pyfly.scheduling.cron import CronExpression


def test_six_field_spring_cron_accepted() -> None:
    # audit #185 — Spring 6-field (seconds-first) + '?' placeholder must parse.
    expr = CronExpression("0 0 12 ? * *")  # noon every day, '?' day-of-month
    nxt = expr.next_fire_time()
    assert nxt is not None


def test_five_field_cron_still_works() -> None:
    expr = CronExpression("*/5 * * * *")
    assert expr.next_fire_time() is not None


def test_sync_time_limiter_honors_fractional_timeout() -> None:
    # audit #184 — a sub-second timeout must actually fire (SIGALRM truncated it).
    @time_limiter(timeout=timedelta(seconds=0.05))
    def slow() -> str:
        time.sleep(0.5)
        return "done"

    with pytest.raises(OperationTimeoutException):
        slow()


def test_sync_time_limiter_allows_fast_call() -> None:
    @time_limiter(timeout=timedelta(seconds=1.0))
    def fast() -> str:
        return "ok"

    assert fast() == "ok"
