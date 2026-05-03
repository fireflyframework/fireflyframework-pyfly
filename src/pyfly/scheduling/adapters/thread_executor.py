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
"""Thread pool task executor adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")


class ThreadPoolTaskExecutor:
    """TaskExecutor using a ThreadPoolExecutor for CPU-bound work.

    Wraps sync functions to run in a thread pool, while async coroutines
    are submitted to the event loop directly.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._tasks: set[asyncio.Task[Any]] = set()

    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Submit a coroutine for execution. Returns an asyncio.Task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def submit_sync(self, func: Callable[..., T], *args: Any) -> asyncio.Task[Any]:
        """Submit a synchronous function to the thread pool."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, func, *args)
        # Wrap the future as a Task-like
        task: asyncio.Task[Any] = asyncio.ensure_future(future)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        """No-op -- thread pool is ready after construction."""

    async def stop(self) -> None:
        """Stop the executor and thread pool, waiting for pending tasks."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._executor.shutdown(wait=True)
