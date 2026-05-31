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
"""Tests for EventCircuitBreaker — open/half-open/closed transitions."""

from __future__ import annotations

import pytest

from pyfly.eda.circuit_breaker import CircuitBreakerConfig, CircuitOpenError, EventCircuitBreaker


async def _fail() -> None:
    raise RuntimeError("boom")


async def _ok() -> str:
    return "ok"


class TestEventCircuitBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = EventCircuitBreaker(CircuitBreakerConfig(failure_threshold=3, recovery_timeout_ms=10_000))
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.execute(_fail)
        # Now OPEN: further calls fast-fail without invoking the handler.
        with pytest.raises(CircuitOpenError):
            await cb.execute(_ok)

    @pytest.mark.asyncio
    async def test_recovers_after_repeated_reopen(self):
        """Regression: the breaker must not get permanently stuck OPEN.

        With recovery_timeout_ms=0 every call is eligible for a half-open trial.
        Before the fix, the half-open counter accumulated across re-opens and the
        breaker raised 'half-open quota exhausted' forever once half_open_max_calls
        total trials had failed.
        """
        cb = EventCircuitBreaker(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_ms=0, half_open_max_calls=2)
        )
        # Trip and re-trip many more times than half_open_max_calls.
        for _ in range(10):
            with pytest.raises(RuntimeError):
                await cb.execute(_fail)
        # After all those failures a successful call must still be able to close it.
        assert await cb.execute(_ok) == "ok"
        # And the breaker is fully closed again.
        assert await cb.execute(_ok) == "ok"
