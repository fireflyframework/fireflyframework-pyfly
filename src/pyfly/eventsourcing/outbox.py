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
"""Transactional outbox — at-least-once delivery of events to a broker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pyfly.eventsourcing.event import StoredEventEnvelope

_logger = logging.getLogger(__name__)


@dataclass
class OutboxRecord:
    """One pending outbox delivery."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    event: StoredEventEnvelope = field(default_factory=StoredEventEnvelope)
    attempts: int = 0
    delivered: bool = False
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class TransactionalOutbox:
    """Stores events in an outbox; a relay coroutine forwards to the broker.

    Args:
        publish: async callable that publishes a single envelope.  Should raise
            on failure so the outbox can retry.
        max_attempts: number of times to retry before marking the record as
            permanently failed.
        poll_interval_s: how often to scan the outbox.
    """

    def __init__(
        self,
        publish: Callable[[StoredEventEnvelope], Awaitable[None]],
        *,
        max_attempts: int = 5,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._publish = publish
        self._max_attempts = max_attempts
        self._poll_interval = poll_interval_s
        self._records: dict[str, OutboxRecord] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def enqueue(self, event: StoredEventEnvelope) -> OutboxRecord:
        record = OutboxRecord(event=event)
        async with self._lock:
            self._records[record.id] = record
        return record

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def pending(self) -> list[OutboxRecord]:
        async with self._lock:
            return [r for r in self._records.values() if not r.delivered and r.attempts < self._max_attempts]

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pending = await self.pending()
                for record in pending:
                    try:
                        await self._publish(record.event)
                        record.delivered = True
                    except Exception as exc:  # noqa: BLE001
                        record.attempts += 1
                        record.last_error = str(exc)
                        _logger.warning("outbox: failed to publish %s: %s", record.id, exc)
            except Exception as exc:  # noqa: BLE001
                _logger.error("outbox loop error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue
