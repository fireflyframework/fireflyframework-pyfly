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
"""Task scheduler engine — discovers @scheduled methods and manages execution loops."""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pyfly.scheduling.adapters.asyncio_executor import AsyncIOTaskExecutor
from pyfly.scheduling.cron import CronExpression
from pyfly.scheduling.ports.outbound import TaskExecutorPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScheduledEntry:
    """Internal record of a discovered @scheduled method and its metadata."""

    bean: Any
    method: Callable[..., Any]
    cron: str | None = None
    fixed_rate: timedelta | None = None
    fixed_delay: timedelta | None = None
    initial_delay: timedelta | None = None


class TaskScheduler:
    """Discovers @scheduled methods on beans and manages their execution loops.

    Usage::

        scheduler = TaskScheduler()
        count = scheduler.discover(beans)
        await scheduler.start()
        # ... application runs ...
        await scheduler.stop()
    """

    def __init__(self, executor: TaskExecutorPort | None = None) -> None:
        self._executor: TaskExecutorPort = executor or AsyncIOTaskExecutor()
        self._running: bool = False
        self._entries: list[_ScheduledEntry] = []
        self._loop_tasks: list[asyncio.Task[Any]] = []

    def discover(self, beans: list[Any]) -> int:
        """Scan beans for @scheduled methods. Return number of scheduled methods found.

        For each bean, inspects all attributes. If an attribute is callable and
        has ``__pyfly_scheduled__ == True``, it is recorded for later scheduling.
        """
        count = 0
        for bean in beans:
            for name in dir(bean):
                if name.startswith("_"):
                    continue
                # Look up statically first so @property / cached_property getters
                # are never evaluated during discovery — a side-effecting or
                # raising property must not break scheduled-task scanning.
                static_attr = inspect.getattr_static(bean, name, None)
                if isinstance(static_attr, (property, functools.cached_property)):
                    continue
                try:
                    attr = getattr(bean, name)
                except Exception:  # noqa: BLE001
                    continue
                if not callable(attr):
                    continue
                if not getattr(attr, "__pyfly_scheduled__", False):
                    continue

                entry = _ScheduledEntry(
                    bean=bean,
                    method=attr,
                    cron=getattr(attr, "__pyfly_scheduled_cron__", None),
                    fixed_rate=getattr(attr, "__pyfly_scheduled_fixed_rate__", None),
                    fixed_delay=getattr(attr, "__pyfly_scheduled_fixed_delay__", None),
                    initial_delay=getattr(attr, "__pyfly_scheduled_initial_delay__", None),
                )
                self._entries.append(entry)
                count += 1
                logger.debug(
                    "Discovered scheduled method %s.%s",
                    type(bean).__name__,
                    name,
                )
        return count

    async def start(self) -> None:
        """Start all scheduling loops. Call after :meth:`discover`."""
        self._running = True
        for entry in self._entries:
            if entry.cron is not None:
                task = asyncio.create_task(self._run_cron_loop(entry.bean, entry.method, entry.cron))
            elif entry.fixed_rate is not None:
                task = asyncio.create_task(
                    self._run_fixed_rate_loop(entry.bean, entry.method, entry.fixed_rate, entry.initial_delay)
                )
            elif entry.fixed_delay is not None:
                task = asyncio.create_task(
                    self._run_fixed_delay_loop(entry.bean, entry.method, entry.fixed_delay, entry.initial_delay)
                )
            else:
                logger.warning(
                    "Scheduled method %s has no trigger — skipping",
                    entry.method,
                )
                continue
            task.add_done_callback(self._loop_done_callback)
            self._loop_tasks.append(task)

    async def stop(self) -> None:
        """Stop all scheduling loops and the executor."""
        self._running = False

        for task in self._loop_tasks:
            task.cancel()

        if self._loop_tasks:
            await asyncio.gather(*self._loop_tasks, return_exceptions=True)

        self._loop_tasks.clear()
        await self._executor.stop()

    @staticmethod
    def _loop_done_callback(task: asyncio.Task[Any]) -> None:
        """Log errors from scheduling loop tasks."""
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("Scheduling loop task failed: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # Private loop methods
    # ------------------------------------------------------------------

    async def _run_cron_loop(self, bean: Any, method: Callable[..., Any], cron_expr: str) -> None:
        """Loop: sleep until next cron fire time, execute method, repeat."""
        cron = CronExpression(cron_expr)
        while self._running:
            delay = cron.seconds_until_next()
            await asyncio.sleep(delay)
            if not self._running:
                break
            await self._executor.submit(self._invoke(bean, method))

    async def _run_fixed_rate_loop(
        self,
        bean: Any,
        method: Callable[..., Any],
        rate: timedelta,
        initial_delay: timedelta | None,
    ) -> None:
        """Loop: execute at fixed intervals regardless of execution time."""
        if initial_delay:
            await asyncio.sleep(initial_delay.total_seconds())
        while self._running:
            await self._executor.submit(self._invoke(bean, method))
            await asyncio.sleep(rate.total_seconds())
            if not self._running:
                break

    async def _run_fixed_delay_loop(
        self,
        bean: Any,
        method: Callable[..., Any],
        delay: timedelta,
        initial_delay: timedelta | None,
    ) -> None:
        """Loop: wait for completion, then wait delay, then execute again."""
        if initial_delay:
            await asyncio.sleep(initial_delay.total_seconds())
        while self._running:
            task = await self._executor.submit(self._invoke(bean, method))
            try:
                await task  # Wait for completion
            except Exception:
                # A failing iteration must not permanently kill the loop —
                # match the fault tolerance of cron/fixed_rate scheduling.
                logger.exception("scheduled fixed_delay task failed; continuing")
            if not self._running:
                break
            await asyncio.sleep(delay.total_seconds())

    @staticmethod
    async def _invoke(bean: Any, method: Callable[..., Any]) -> None:
        """Invoke a scheduled method, handling both sync and async methods."""
        result = method()
        if inspect.isawaitable(result):
            await result
