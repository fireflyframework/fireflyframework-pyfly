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
"""Cron / fixed-rate / fixed-delay scheduler for ``@scheduled_*`` saga/workflow/TCC.

``croniter`` is used when installed; cron-style entries are silently skipped
otherwise so the engine works without it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

_croniter: Any = None
_HAS_CRONITER = False
try:
    from croniter import croniter as _croniter_imported  # type: ignore[import-untyped, import-not-found, unused-ignore]

    _croniter = _croniter_imported
    _HAS_CRONITER = True
except Exception:  # noqa: BLE001
    pass

_logger = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    """One scheduled trigger registered with :class:`OrchestrationScheduler`."""

    id: str
    callback: Callable[[], Awaitable[None]]
    cron: str | None = None
    fixed_rate_ms: int | None = None
    fixed_delay_ms: int | None = None
    initial_delay_ms: int = 0
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    runs: int = 0
    failures: int = 0
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def has_valid_trigger(self) -> bool:
        return bool(self.cron or self.fixed_rate_ms or self.fixed_delay_ms)


class OrchestrationScheduler:
    """Manages periodic orchestration triggers."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._stop_event = asyncio.Event()
        self._running = False

    def register(self, task: ScheduledTask) -> None:
        if not task.has_valid_trigger():
            msg = f"scheduled task '{task.id}' must define cron, fixed_rate_ms or fixed_delay_ms"
            raise ValueError(msg)
        self._tasks[task.id] = task
        # If the scheduler is already running (the context lifecycle starts it
        # before orchestration beans are post-processed), spin the loop up now
        # so tasks registered post-start still fire (audit #54).
        if self._running and task.enabled and task._task is None:
            task._task = asyncio.create_task(self._run_loop(task))

    def unregister(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task is not None and task._task is not None:
            task._task.cancel()

    def list(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        for task in self._tasks.values():
            if task.enabled:
                task._task = asyncio.create_task(self._run_loop(task))

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        for task in self._tasks.values():
            if task._task is not None:
                task._task.cancel()
        await asyncio.gather(
            *(t._task for t in self._tasks.values() if t._task is not None),
            return_exceptions=True,
        )
        for task in self._tasks.values():
            task._task = None

    async def _run_loop(self, task: ScheduledTask) -> None:
        if task.initial_delay_ms > 0:
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=task.initial_delay_ms / 1000.0)
                return
            except TimeoutError:
                pass

        while not self._stop_event.is_set():
            sleep_seconds = self._compute_next_delay(task)
            task.next_run_at = datetime.now(UTC) + timedelta(seconds=sleep_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_seconds)
                return
            except TimeoutError:
                pass

            try:
                await task.callback()
                task.runs += 1
                task.last_run_at = datetime.now(UTC)
            except Exception as exc:  # noqa: BLE001
                task.failures += 1
                _logger.error("scheduled task '%s' failed: %s", task.id, exc)

            if task.fixed_delay_ms is not None:
                # fixed-delay: count from completion of last run
                continue

    @staticmethod
    def _compute_next_delay(task: ScheduledTask) -> float:
        if task.fixed_rate_ms is not None:
            return task.fixed_rate_ms / 1000.0
        if task.fixed_delay_ms is not None:
            return task.fixed_delay_ms / 1000.0
        if task.cron is not None and _HAS_CRONITER:
            base = datetime.now(UTC)
            iter_ = _croniter(task.cron, base)
            next_dt: datetime = iter_.get_next(datetime)
            return max(0.0, float((next_dt - base).total_seconds()))
        # Cron requested but croniter missing — back off long enough that the
        # admin notices when checking logs.
        _logger.warning("croniter not installed; cron task '%s' is inactive", task.id)
        return 3600.0
