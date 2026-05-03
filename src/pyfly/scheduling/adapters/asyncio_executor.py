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
"""AsyncIO task executor adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncIOTaskExecutor:
    """Default TaskExecutor using asyncio.create_task."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Submit a coroutine for execution. Returns an asyncio.Task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        """No-op -- asyncio executor is ready after construction."""

    async def stop(self) -> None:
        """Stop the executor, waiting for pending tasks to complete."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
