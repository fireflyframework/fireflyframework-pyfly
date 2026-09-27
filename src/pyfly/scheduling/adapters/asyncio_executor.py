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

from pyfly.data.transaction import detached
from pyfly.data.transaction.template import run_shielded

T = TypeVar("T")


async def drain(tasks: set[asyncio.Task[Any]]) -> None:
    """Wait until every task in *tasks* (a live set: tasks submitted meanwhile count too) has ended.

    When the caller is cancelled (a shutdown timeout), the tasks still running are cancelled and awaited,
    whatever further cancellations arrive, before the cancellation propagates.
    """
    while pending := {task for task in tasks if not task.done()}:
        try:
            await asyncio.wait(pending)
        except asyncio.CancelledError:
            for task in pending:
                task.cancel()
            await run_shielded(asyncio.wait(pending))
            raise


class AsyncIOTaskExecutor:
    """Default TaskExecutor: each submitted coroutine runs in an asyncio task of its own.

    The task is started with the transaction state cleared (:func:`pyfly.data.transaction.detached`): the work
    gets its own units of work and never joins the submitter's, which may end before it does.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    async def submit(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        """Submit a coroutine for execution. Returns an asyncio.Task."""
        task = detached(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        """No-op -- asyncio executor is ready after construction."""

    async def stop(self) -> None:
        """Stop the executor, waiting for pending tasks to complete (cancelling them if the wait is cancelled)."""
        await drain(self._tasks)
        self._tasks.clear()
