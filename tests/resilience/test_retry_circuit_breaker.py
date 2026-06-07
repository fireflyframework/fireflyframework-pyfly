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
"""@retry + @circuit_breaker decorators (v26.06.36) — the resilience capability the
docs advertised but did not ship (final parity audit)."""

from __future__ import annotations

import pytest

from pyfly.kernel.exceptions import CircuitBreakerException
from pyfly.resilience import CircuitBreaker, CircuitState, circuit_breaker, retry


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    @retry(max_attempts=3)
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_exhausts_and_reraises_last() -> None:
    calls = {"n": 0}

    @retry(max_attempts=2)
    async def always_fails() -> None:
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await always_fails()
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_only_retries_listed_exceptions() -> None:
    calls = {"n": 0}

    @retry(max_attempts=3, exceptions=(KeyError,))
    async def raises_value() -> None:
        calls["n"] += 1
        raise ValueError("not retried")

    with pytest.raises(ValueError):
        await raises_value()
    assert calls["n"] == 1  # ValueError not in exceptions -> no retry


def test_retry_sync() -> None:
    calls = {"n": 0}

    @retry(max_attempts=2)
    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("x")
        return 42

    assert flaky() == 42
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_rejects() -> None:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

    @circuit_breaker(cb)
    async def failing() -> None:
        raise ValueError("boom")

    for _ in range(2):
        with pytest.raises(ValueError):
            await failing()
    assert cb.state is CircuitState.OPEN

    # Open circuit rejects without invoking the function.
    with pytest.raises(CircuitBreakerException):
        await failing()


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_through_half_open() -> None:
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
    state = {"fail": True}

    @circuit_breaker(cb)
    async def svc() -> str:
        if state["fail"]:
            raise ValueError("boom")
        return "ok"

    with pytest.raises(ValueError):
        await svc()  # trips OPEN (threshold 1)

    state["fail"] = False
    assert await svc() == "ok"  # recovery_timeout=0 -> HALF_OPEN trial succeeds -> CLOSED
    assert cb.state is CircuitState.CLOSED
