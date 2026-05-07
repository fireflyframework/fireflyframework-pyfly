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
"""Recovery service — finds stale executions on restart and triggers cleanup."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from pyfly.transactional.core.persistence import (
    ExecutionPersistenceProvider,
    ExecutionState,
)

_logger = logging.getLogger(__name__)


class RecoveryService:
    """Periodically inspects persisted state to surface stuck executions.

    Args:
        persistence: backend the engine uses.
        stale_threshold: executions whose ``updated_at`` is older than this are
            considered stale.
        retention_period: terminal executions older than this are deleted.
        scan_interval: time between background scans.
    """

    def __init__(
        self,
        persistence: ExecutionPersistenceProvider,
        *,
        stale_threshold: timedelta = timedelta(hours=1),
        retention_period: timedelta = timedelta(days=7),
        scan_interval: timedelta = timedelta(hours=1),
    ) -> None:
        self._persistence = persistence
        self._stale_threshold = stale_threshold
        self._retention_period = retention_period
        self._scan_interval = scan_interval
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def find_stale(self) -> list[ExecutionState]:
        cutoff = datetime.now(UTC) - self._stale_threshold
        return await self._persistence.find_stale(cutoff)

    async def cleanup(self) -> int:
        return await self._persistence.cleanup(self._retention_period)

    async def start(self) -> None:
        """Begin periodic background scans."""
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                stale = await self.find_stale()
                if stale:
                    _logger.warning("recovery: %d stale execution(s) found", len(stale))
                    for s in stale:
                        _logger.warning(
                            "  stale %s/%s status=%s updated_at=%s",
                            s.name,
                            s.correlation_id,
                            s.status.value,
                            s.updated_at,
                        )
                cleaned = await self.cleanup()
                if cleaned:
                    _logger.info("recovery: cleaned %d completed execution(s)", cleaned)
            except Exception as exc:  # noqa: BLE001
                _logger.error("recovery scan failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._scan_interval.total_seconds()
                )
            except TimeoutError:
                continue
