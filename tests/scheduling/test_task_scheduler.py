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
"""Tests for TaskScheduler engine."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from pyfly.scheduling.decorators import scheduled
from pyfly.scheduling.task_scheduler import TaskScheduler

# ---------------------------------------------------------------------------
# Helper beans used across tests
# ---------------------------------------------------------------------------


class _FixedRateBean:
    """Bean with a fixed-rate scheduled method that tracks invocations."""

    def __init__(self) -> None:
        self.call_count = 0

    @scheduled(fixed_rate=timedelta(seconds=0.05))
    async def tick(self) -> None:
        self.call_count += 1


class _FixedDelayBean:
    """Bean with a fixed-delay scheduled method that tracks invocations."""

    def __init__(self) -> None:
        self.call_count = 0

    @scheduled(fixed_delay=timedelta(seconds=0.05))
    async def tick(self) -> None:
        self.call_count += 1


class _CronBean:
    """Bean with a cron-scheduled method."""

    def __init__(self) -> None:
        self.call_count = 0

    @scheduled(cron="* * * * *")
    async def every_minute(self) -> None:
        self.call_count += 1


class _PlainBean:
    """Bean with no scheduled methods."""

    def do_stuff(self) -> str:
        return "stuff"


class _SyncScheduledBean:
    """Bean with a synchronous scheduled method."""

    def __init__(self) -> None:
        self.call_count = 0

    @scheduled(fixed_rate=timedelta(seconds=0.05))
    def sync_tick(self) -> None:
        self.call_count += 1


class _InitialDelayBean:
    """Bean with an initial-delay scheduled method."""

    def __init__(self) -> None:
        self.call_count = 0

    @scheduled(fixed_rate=timedelta(seconds=0.05), initial_delay=timedelta(seconds=0.1))
    async def delayed_tick(self) -> None:
        self.call_count += 1


class _MultiMethodBean:
    """Bean with multiple scheduled methods."""

    def __init__(self) -> None:
        self.rate_count = 0
        self.delay_count = 0

    @scheduled(fixed_rate=timedelta(seconds=0.05))
    async def rate_tick(self) -> None:
        self.rate_count += 1

    @scheduled(fixed_delay=timedelta(seconds=0.05))
    async def delay_tick(self) -> None:
        self.delay_count += 1


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_finds_scheduled_methods(self) -> None:
        """discover() finds methods decorated with @scheduled."""
        bean = _FixedRateBean()
        scheduler = TaskScheduler()
        count = scheduler.discover([bean])
        assert count == 1

    def test_discover_ignores_undecorated_methods(self) -> None:
        """discover() returns 0 for beans with no @scheduled methods."""
        bean = _PlainBean()
        scheduler = TaskScheduler()
        count = scheduler.discover([bean])
        assert count == 0

    def test_discover_multiple_beans(self) -> None:
        """discover() finds scheduled methods across multiple beans."""
        rate_bean = _FixedRateBean()
        delay_bean = _FixedDelayBean()
        multi_bean = _MultiMethodBean()
        scheduler = TaskScheduler()
        count = scheduler.discover([rate_bean, delay_bean, multi_bean])
        assert count == 4  # 1 + 1 + 2

    def test_discover_returns_zero_for_empty(self) -> None:
        """discover() returns 0 when given an empty list."""
        scheduler = TaskScheduler()
        count = scheduler.discover([])
        assert count == 0

    def test_discover_multi_method_bean(self) -> None:
        """discover() finds all scheduled methods on a single bean."""
        bean = _MultiMethodBean()
        scheduler = TaskScheduler()
        count = scheduler.discover([bean])
        assert count == 2


# ---------------------------------------------------------------------------
# Execution tests
# ---------------------------------------------------------------------------


class TestExecution:
    @pytest.mark.asyncio
    async def test_fixed_rate_executes_periodically(self) -> None:
        """Fixed-rate loop invokes the method multiple times."""
        bean = _FixedRateBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        # Wait long enough for several invocations (rate=0.05s)
        await asyncio.sleep(0.18)
        await scheduler.stop()

        # Should have been called at least 2 times
        assert bean.call_count >= 2

    @pytest.mark.asyncio
    async def test_fixed_delay_waits_after_completion(self) -> None:
        """Fixed-delay loop waits for completion before starting the delay."""

        class SlowBean:
            def __init__(self) -> None:
                self.call_count = 0
                self.timestamps: list[float] = []

            @scheduled(fixed_delay=timedelta(seconds=0.05))
            async def slow_tick(self) -> None:
                self.call_count += 1
                # Simulate work that takes 0.05s
                await asyncio.sleep(0.05)

        bean = SlowBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        # With 0.05s work + 0.05s delay = ~0.1s per cycle
        # In 0.25s we expect ~2 calls (not 5 if it were fixed_rate ignoring work time)
        await asyncio.sleep(0.25)
        await scheduler.stop()

        assert bean.call_count >= 2
        assert bean.call_count <= 3  # Should not be more with delay-after-completion

    @pytest.mark.asyncio
    async def test_cron_loop_executes_on_schedule(self) -> None:
        """Cron loop fires the method. Uses mocking to avoid waiting a full minute."""
        bean = _CronBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])

        # For a real cron test, we'd need to wait up to 60s.
        # Instead, test the loop method directly with a very short mock sleep.
        # We'll start and quickly stop to verify the loop task is created,
        # then test _run_cron_loop in isolation by patching CronExpression.
        from unittest.mock import patch

        with patch("pyfly.scheduling.task_scheduler.CronExpression") as mock_cron_cls:
            mock_cron_instance = mock_cron_cls.return_value
            # Return very short delay so the loop fires quickly
            mock_cron_instance.seconds_until_next.return_value = 0.01

            scheduler2 = TaskScheduler()
            scheduler2.discover([bean])
            await scheduler2.start()
            await asyncio.sleep(0.1)
            await scheduler2.stop()

        assert bean.call_count >= 1

    @pytest.mark.asyncio
    async def test_initial_delay_honored(self) -> None:
        """Initial delay postpones the first execution."""
        bean = _InitialDelayBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        # After 0.05s the initial delay (0.1s) hasn't elapsed yet
        await asyncio.sleep(0.05)
        assert bean.call_count == 0

        # After 0.2s total, initial delay has elapsed and at least one tick happened
        await asyncio.sleep(0.15)
        await scheduler.stop()
        assert bean.call_count >= 1

    @pytest.mark.asyncio
    async def test_sync_method_scheduled(self) -> None:
        """A synchronous (non-async) scheduled method is invoked correctly."""
        bean = _SyncScheduledBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        await asyncio.sleep(0.18)
        await scheduler.stop()

        assert bean.call_count >= 2


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_loop_tasks(self) -> None:
        """After start(), loop tasks exist in _loop_tasks."""
        bean = _FixedRateBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        assert len(scheduler._loop_tasks) == 1

        await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_loops(self) -> None:
        """After stop(), all loop tasks are cancelled and cleared."""
        bean = _FixedRateBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        assert len(scheduler._loop_tasks) == 1
        await scheduler.stop()

        # _loop_tasks should be cleared after stop()
        assert len(scheduler._loop_tasks) == 0
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_stop_shuts_down_executor(self) -> None:
        """stop() calls executor.stop()."""
        mock_executor = AsyncMock()

        async def _mock_submit(coro: object) -> asyncio.Task[None]:
            """Run the coroutine so it doesn't leak, then return a resolved task."""
            task = asyncio.create_task(coro)  # type: ignore[arg-type]
            return task

        mock_executor.submit = AsyncMock(side_effect=_mock_submit)

        scheduler = TaskScheduler(executor=mock_executor)
        bean = _FixedRateBean()
        scheduler.discover([bean])
        await scheduler.start()

        # Let it run briefly
        await asyncio.sleep(0.02)
        await scheduler.stop()

        mock_executor.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_with_no_started_tasks(self) -> None:
        """stop() works gracefully when no tasks have been started."""
        scheduler = TaskScheduler()
        # Don't discover or start anything
        await scheduler.stop()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_multiple_loop_tasks_for_multi_method_bean(self) -> None:
        """Each scheduled method gets its own loop task."""
        bean = _MultiMethodBean()
        scheduler = TaskScheduler()
        scheduler.discover([bean])
        await scheduler.start()

        assert len(scheduler._loop_tasks) == 2

        await scheduler.stop()
