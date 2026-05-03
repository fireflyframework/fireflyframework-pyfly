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
"""Tests for bulkhead concurrency limiter."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.kernel.exceptions import BulkheadException
from pyfly.resilience.bulkhead import Bulkhead, bulkhead


class TestBulkhead:
    async def test_acquire_within_capacity(self) -> None:
        """Can acquire up to max_concurrent without exception."""
        bh = Bulkhead(max_concurrent=2)
        await bh.acquire()
        await bh.acquire()
        # Both acquired successfully — no exception raised.

    async def test_acquire_at_capacity_raises(self) -> None:
        """Raises BulkheadException when all slots are taken."""
        bh = Bulkhead(max_concurrent=2)
        await bh.acquire()
        await bh.acquire()

        with pytest.raises(BulkheadException):
            await bh.acquire()

    async def test_release_frees_slot(self) -> None:
        """After release, can acquire again."""
        bh = Bulkhead(max_concurrent=1)
        await bh.acquire()

        with pytest.raises(BulkheadException):
            await bh.acquire()

        bh.release()
        # Should succeed now that a slot is free.
        await bh.acquire()

    async def test_available_slots_property(self) -> None:
        """Reflects current available count."""
        bh = Bulkhead(max_concurrent=3)
        assert bh.available_slots == 3

        await bh.acquire()
        assert bh.available_slots == 2

        await bh.acquire()
        assert bh.available_slots == 1

        bh.release()
        assert bh.available_slots == 2


class TestBulkheadDecorator:
    async def test_decorator_limits_concurrency(self) -> None:
        """Multiple concurrent decorated calls: within limit succeed, excess raises."""
        bh = Bulkhead(max_concurrent=2)
        hold = asyncio.Event()

        @bulkhead(bh)
        async def slow_work() -> str:
            await hold.wait()
            return "done"

        # Start 2 tasks (fills bulkhead).
        task1 = asyncio.create_task(slow_work())
        task2 = asyncio.create_task(slow_work())
        await asyncio.sleep(0.01)  # Let them acquire slots.

        # 3rd call should fail immediately.
        with pytest.raises(BulkheadException):
            await slow_work()

        # Release blocked tasks.
        hold.set()
        assert await task1 == "done"
        assert await task2 == "done"

    async def test_decorator_releases_on_completion(self) -> None:
        """After decorated call completes, slot is freed."""
        bh = Bulkhead(max_concurrent=1)

        @bulkhead(bh)
        async def work() -> str:
            return "ok"

        assert bh.available_slots == 1
        result = await work()
        assert result == "ok"
        assert bh.available_slots == 1  # Slot released after completion.

    async def test_decorator_releases_on_exception(self) -> None:
        """If decorated call raises, slot is still freed."""
        bh = Bulkhead(max_concurrent=1)

        @bulkhead(bh)
        async def failing_work() -> None:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await failing_work()

        # Slot must be released despite the exception.
        assert bh.available_slots == 1
