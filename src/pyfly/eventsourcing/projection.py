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
"""Projections — read-model builders that consume the event log."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.store import EventStore

_logger = logging.getLogger(__name__)


@runtime_checkable
class Projection(Protocol):
    """A projection consumes events from the store and updates a read model."""

    name: str

    async def handle(self, event: StoredEventEnvelope) -> None: ...


class ProjectionRunner:
    """Polls the event store and feeds new events to a projection."""

    def __init__(
        self,
        projection: Projection,
        store: EventStore,
        *,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._projection = projection
        self._store = store
        self._poll_interval = poll_interval_s
        self._last_event_id: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

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

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                events = await self._store.stream_all(after_event_id=self._last_event_id, limit=100)
                for evt in events:
                    try:
                        await self._projection.handle(evt)
                        self._last_event_id = evt.event_id
                    except Exception as exc:  # noqa: BLE001
                        _logger.error(
                            "projection %s failed on event %s: %s",
                            self._projection.name,
                            evt.event_id,
                            exc,
                        )
            except Exception as exc:  # noqa: BLE001
                _logger.error("projection runner error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue


class FunctionProjection:
    """Quick projection wrapper around a single async callable."""

    def __init__(self, name: str, handler: Callable[[StoredEventEnvelope], Awaitable[None]]) -> None:
        self.name = name
        self._handler = handler

    async def handle(self, event: StoredEventEnvelope) -> None:
        await self._handler(event)
