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
"""In-memory event bus for testing and single-process applications."""

from __future__ import annotations

import fnmatch
from typing import Any

from pyfly.eda.ports.outbound import EventHandler
from pyfly.eda.types import EventEnvelope


class InMemoryEventBus:
    """In-memory event bus using pattern-matched subscriptions.

    Supports wildcard patterns (e.g. "user.*" matches "user.created").
    """

    def __init__(self) -> None:
        self._handlers: list[tuple[str, EventHandler]] = []

    def subscribe(self, event_type_pattern: str, handler: EventHandler) -> None:
        """Subscribe a handler to events matching the pattern."""
        self._handlers.append((event_type_pattern, handler))

    async def publish(
        self,
        destination: str,
        event_type: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Publish an event to all matching subscribers."""
        envelope = EventEnvelope(
            event_type=event_type,
            payload=payload,
            destination=destination,
            headers=headers or {},
        )
        for pattern, handler in self._handlers:
            if fnmatch.fnmatch(event_type, pattern):
                await handler(envelope)

    async def start(self) -> None:
        """No-op for in-memory event bus."""

    async def stop(self) -> None:
        """No-op for in-memory event bus."""
