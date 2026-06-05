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
"""Regression: a synchronous @scheduled task is offloaded to a thread so it does
not block the event loop (v26.06.17).

Previously TaskScheduler._invoke ran ``method()`` inline on the loop, so a blocking
sync scheduled task (I/O, ``time.sleep``) stalled the entire application for its
duration. Sync methods are now run via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from pyfly.scheduling import TaskScheduler, scheduled


class _Jobs:
    def __init__(self) -> None:
        self.ticks = 0

    @scheduled(fixed_rate=timedelta(seconds=0.02))
    async def heartbeat(self) -> None:
        self.ticks += 1

    @scheduled(fixed_rate=timedelta(seconds=5))  # fires once at t~0 within our window
    def blocking(self) -> None:
        time.sleep(0.4)  # synchronous blocking body


@pytest.mark.asyncio
async def test_sync_task_does_not_block_event_loop() -> None:
    jobs = _Jobs()
    scheduler = TaskScheduler()
    assert scheduler.discover([jobs]) == 2
    await scheduler.start()
    try:
        # The blocking sync task (0.4s) fires immediately. If it ran inline on the
        # loop, the 20ms async heartbeat could not tick during the first 0.2s.
        await asyncio.sleep(0.2)
        early_ticks = jobs.ticks
    finally:
        await scheduler.stop()

    assert early_ticks >= 4, f"event loop blocked by sync task: only {early_ticks} heartbeat ticks in 0.2s"


class _SyncJob:
    def __init__(self) -> None:
        self.count = 0

    @scheduled(fixed_rate=timedelta(seconds=0.02))
    def tick(self) -> None:
        self.count += 1


@pytest.mark.asyncio
async def test_sync_task_still_executes() -> None:
    job = _SyncJob()
    scheduler = TaskScheduler()
    scheduler.discover([job])
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await scheduler.stop()

    assert job.count >= 3  # offloaded to a thread, but still runs repeatedly
