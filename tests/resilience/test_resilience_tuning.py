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
"""Resilience tuning (v26.06.43): @retry jitter, circuit-breaker failure-rate window
and half-open probe budget."""

from __future__ import annotations

import pytest

from pyfly.resilience import CircuitBreaker, CircuitState, retry


@pytest.mark.asyncio
async def test_retry_with_jitter_still_succeeds() -> None:
    calls = {"n": 0}

    @retry(max_attempts=3, delay=0.001, jitter=0.5)
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


def test_failure_rate_window_opens_on_rate() -> None:
    cb = CircuitBreaker(failure_rate_threshold=0.5, window_size=4)
    # Partial window (3 calls) never trips, even with failures.
    cb.on_success()
    cb.on_failure()
    cb.on_success()
    assert cb.state is CircuitState.CLOSED
    # 4th call completes the window [S, F, S, F] -> 50% failures -> open.
    cb.on_failure()
    assert cb.state is CircuitState.OPEN


def test_half_open_requires_configured_successes() -> None:
    # Private _state is read directly: the `state` property runs _maybe_half_open,
    # which would reset the half-open counters mid-sequence.
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0, half_open_max_calls=2)
    cb.on_failure()  # consecutive threshold 1 -> OPEN
    cb.before_call()  # recovery_timeout=0 -> HALF_OPEN, probe 1
    cb.on_success()  # 1 success < 2 -> still probing
    assert cb._state is CircuitState.HALF_OPEN
    cb.before_call()  # probe 2
    cb.on_success()  # 2 successes >= 2 -> CLOSED
    assert cb._state is CircuitState.CLOSED


def test_half_open_failure_reopens_immediately() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0, half_open_max_calls=3)
    cb.on_failure()  # OPEN
    cb.before_call()  # HALF_OPEN, probe 1
    cb.on_failure()  # any half-open failure -> OPEN
    assert cb._state is CircuitState.OPEN
