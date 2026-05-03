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
"""Tests for HTTP client, circuit breaker, and retry."""

from datetime import timedelta

import pytest

from pyfly.client.circuit_breaker import CircuitBreaker, CircuitState
from pyfly.client.retry import RetryPolicy
from pyfly.kernel.exceptions import CircuitBreakerException


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=timedelta(seconds=30))
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_stays_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=timedelta(seconds=30))

        async def success():
            return "ok"

        result = await cb.call(success)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=timedelta(seconds=30))

        async def fail():
            raise ConnectionError("down")

        for _ in range(2):
            with pytest.raises(ConnectionError):
                await cb.call(fail)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_rejects_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=timedelta(seconds=30))

        async def fail():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await cb.call(fail)

        assert cb.state == CircuitState.OPEN

        with pytest.raises(CircuitBreakerException, match="Circuit breaker is open"):
            await cb.call(fail)

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=timedelta(milliseconds=50))

        async def fail():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await cb.call(fail)
        assert cb.state == CircuitState.OPEN

        import asyncio

        await asyncio.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_success(self):
        import asyncio

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=timedelta(milliseconds=50))

        async def fail():
            raise ConnectionError("down")

        async def succeed():
            return "ok"

        with pytest.raises(ConnectionError):
            await cb.call(fail)

        await asyncio.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN

        result = await cb.call(succeed)
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=timedelta(seconds=30))

        async def fail():
            raise ConnectionError("down")

        async def succeed():
            return "ok"

        # 2 failures, then a success
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await cb.call(fail)
        await cb.call(succeed)

        # 2 more failures should not open (count was reset)
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await cb.call(fail)
        assert cb.state == CircuitState.CLOSED


class TestRetryPolicy:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        policy = RetryPolicy(max_attempts=3, base_delay=timedelta(milliseconds=10))
        call_count = 0

        async def operation():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await policy.execute(operation)
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        policy = RetryPolicy(max_attempts=3, base_delay=timedelta(milliseconds=10))
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("flaky")
            return "ok"

        result = await policy.execute(flaky)
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self):
        policy = RetryPolicy(max_attempts=2, base_delay=timedelta(milliseconds=10))

        async def always_fail():
            raise ConnectionError("permanent failure")

        with pytest.raises(ConnectionError, match="permanent failure"):
            await policy.execute(always_fail)

    @pytest.mark.asyncio
    async def test_only_retries_matching_exceptions(self):
        policy = RetryPolicy(
            max_attempts=3,
            base_delay=timedelta(milliseconds=10),
            retry_on=(ConnectionError,),
        )
        call_count = 0

        async def type_error():
            nonlocal call_count
            call_count += 1
            raise TypeError("not retryable")

        with pytest.raises(TypeError):
            await policy.execute(type_error)
        assert call_count == 1  # No retry for TypeError
