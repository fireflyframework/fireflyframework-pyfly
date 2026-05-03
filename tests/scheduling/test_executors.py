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
"""Tests for TaskExecutor port and adapters."""

from __future__ import annotations

import asyncio

import pytest

from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
from pyfly.scheduling.adapters.thread_executor import ThreadPoolTaskExecutor
from pyfly.scheduling.ports.outbound import TaskExecutorPort


class TestAsyncIOTaskExecutor:
    @pytest.mark.asyncio
    async def test_submit_runs_coroutine_and_returns_result(self) -> None:
        executor = AsyncIOTaskExecutor()

        async def add(a: int, b: int) -> int:
            return a + b

        task = await executor.submit(add(2, 3))
        result = await task
        assert result == 5

    @pytest.mark.asyncio
    async def test_submit_tracks_tasks(self) -> None:
        executor = AsyncIOTaskExecutor()
        event = asyncio.Event()

        async def wait_for_event() -> str:
            await event.wait()
            return "done"

        task = await executor.submit(wait_for_event())
        # Task should be tracked while pending
        assert task in executor._tasks

        event.set()
        await task
        # After completion, the done callback should have removed it
        assert task not in executor._tasks

    @pytest.mark.asyncio
    async def test_stop_waits_for_all_tasks(self) -> None:
        executor = AsyncIOTaskExecutor()
        results: list[int] = []

        async def append_after_delay(value: int) -> None:
            await asyncio.sleep(0.01)
            results.append(value)

        await executor.submit(append_after_delay(1))
        await executor.submit(append_after_delay(2))
        await executor.submit(append_after_delay(3))

        await executor.stop()
        assert sorted(results) == [1, 2, 3]


class TestThreadPoolTaskExecutor:
    @pytest.mark.asyncio
    async def test_submit_runs_coroutine_and_returns_result(self) -> None:
        executor = ThreadPoolTaskExecutor(max_workers=2)

        async def multiply(a: int, b: int) -> int:
            return a * b

        task = await executor.submit(multiply(3, 4))
        result = await task
        assert result == 12
        await executor.stop()

    @pytest.mark.asyncio
    async def test_submit_sync_runs_function_in_thread_pool(self) -> None:
        executor = ThreadPoolTaskExecutor(max_workers=2)

        def blocking_add(a: int, b: int) -> int:
            import time

            time.sleep(0.01)  # Simulate blocking work
            return a + b

        task = executor.submit_sync(blocking_add, 5, 7)
        result = await task
        assert result == 12
        await executor.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_thread_pool(self) -> None:
        executor = ThreadPoolTaskExecutor(max_workers=2)

        async def simple() -> str:
            return "hello"

        await executor.submit(simple())
        await executor.stop()

        assert len(executor._tasks) == 0
        # ThreadPoolExecutor should be shut down — submitting should raise
        with pytest.raises(RuntimeError):
            executor._executor.submit(lambda: None)


class TestTaskExecutorPort:
    def test_asyncio_executor_satisfies_protocol(self) -> None:
        executor = AsyncIOTaskExecutor()
        assert isinstance(executor, TaskExecutorPort)

    def test_thread_pool_executor_satisfies_protocol(self) -> None:
        executor = ThreadPoolTaskExecutor()
        assert isinstance(executor, TaskExecutorPort)
