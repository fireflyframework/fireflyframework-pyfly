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
"""Tests for time limiter decorator."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from pyfly.kernel.exceptions import OperationTimeoutException
from pyfly.resilience.time_limiter import time_limiter


class TestTimeLimiter:
    async def test_fast_call_succeeds(self) -> None:
        @time_limiter(timeout=timedelta(seconds=0.1))
        async def fast_op() -> str:
            await asyncio.sleep(0.01)
            return "done"

        result = await fast_op()
        assert result == "done"

    async def test_slow_call_times_out(self) -> None:
        @time_limiter(timeout=timedelta(seconds=0.05))
        async def slow_op() -> str:
            await asyncio.sleep(1.0)
            return "done"

        with pytest.raises(OperationTimeoutException):
            await slow_op()

    async def test_timeout_message_includes_function_name(self) -> None:
        @time_limiter(timeout=timedelta(seconds=0.05))
        async def my_slow_function() -> None:
            await asyncio.sleep(1.0)

        with pytest.raises(OperationTimeoutException, match="my_slow_function"):
            await my_slow_function()

    async def test_return_value_preserved(self) -> None:
        @time_limiter(timeout=timedelta(seconds=0.1))
        async def compute() -> dict[str, int]:
            await asyncio.sleep(0.01)
            return {"answer": 42}

        result = await compute()
        assert result == {"answer": 42}

    async def test_exception_preserved(self) -> None:
        @time_limiter(timeout=timedelta(seconds=0.1))
        async def failing_op() -> None:
            raise ValueError("something went wrong")

        with pytest.raises(ValueError, match="something went wrong"):
            await failing_op()
