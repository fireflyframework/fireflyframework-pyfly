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
"""Domain event publisher for CQRS commands.

Mirrors Java's ``CommandEventPublisher`` / ``EdaCommandEventPublisher``.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

_logger = logging.getLogger(__name__)


@runtime_checkable
class CommandEventPublisher(Protocol):
    """Publishes domain events produced by command handlers."""

    async def publish(self, event: Any, *, destination: str | None = None) -> None: ...


class NoOpEventPublisher:
    """Silent publisher — used when no EDA integration is configured."""

    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        _logger.debug("NoOp: event %s not published (no EDA configured)", type(event).__name__)


class EdaCommandEventPublisher:
    """Event publisher backed by pyfly's EDA messaging subsystem.

    Delegates to the messaging ``Producer`` from ``pyfly.messaging``.
    """

    def __init__(self, producer: Any, default_destination: str = "cqrs.events") -> None:
        self._producer = producer
        self._default_destination = default_destination

    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        target = destination or self._default_destination
        try:
            await self._producer.send(target, event)
            _logger.debug("Published event %s to %s", type(event).__name__, target)
        except Exception as exc:
            _logger.error("Failed to publish event %s to %s: %s", type(event).__name__, target, exc)
            raise
