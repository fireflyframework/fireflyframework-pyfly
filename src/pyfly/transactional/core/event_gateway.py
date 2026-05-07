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
"""Event gateway — routes inbound domain events to saga/workflow/TCC starts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass
class EventTrigger:
    """One subscription entry inside the gateway."""

    event_type: str
    handler: Callable[[Any], Awaitable[Any]]
    target: str


class EventGateway:
    """Maps event-type strings to orchestration entry points.

    The gateway decouples external event consumers (Kafka, RabbitMQ, in-memory
    bus) from the engine — adapters just call :meth:`dispatch`.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, list[EventTrigger]] = {}
        self._lock = asyncio.Lock()

    async def register(self, event_type: str, target: str, handler: Callable[[Any], Awaitable[Any]]) -> None:
        async with self._lock:
            self._subscriptions.setdefault(event_type, []).append(
                EventTrigger(event_type=event_type, handler=handler, target=target)
            )

    async def unregister(self, target: str) -> None:
        async with self._lock:
            for event_type, triggers in list(self._subscriptions.items()):
                self._subscriptions[event_type] = [t for t in triggers if t.target != target]
                if not self._subscriptions[event_type]:
                    del self._subscriptions[event_type]

    async def dispatch(self, event_type: str, payload: Any) -> list[Any]:
        async with self._lock:
            triggers = list(self._subscriptions.get(event_type, []))
        results: list[Any] = []
        for trigger in triggers:
            try:
                results.append(await trigger.handler(payload))
            except Exception as exc:  # noqa: BLE001
                _logger.exception("event gateway: handler for '%s' failed: %s", event_type, exc)
        return results

    def list_subscriptions(self) -> dict[str, list[str]]:
        return {evt: [t.target for t in triggers] for evt, triggers in self._subscriptions.items()}
