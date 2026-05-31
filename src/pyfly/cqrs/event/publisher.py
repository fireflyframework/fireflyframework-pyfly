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
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pyfly.eda.ports.outbound import EventPublisher

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
    """Event publisher backed by pyfly's EDA :class:`EventPublisher`.

    Delegates to the EDA :class:`~pyfly.eda.ports.outbound.EventPublisher`
    port, adapting each domain event to that port's
    ``publish(destination, event_type, payload, headers)`` contract:

    * ``event_type`` is taken from the event's ``event_type`` attribute when
      present, otherwise the event class name.
    * ``payload`` is the event serialised to a ``dict`` — via
      :func:`dataclasses.asdict` for dataclasses, else ``__dict__``.
    """

    def __init__(self, producer: EventPublisher, default_destination: str = "cqrs.events") -> None:
        self._producer = producer
        self._default_destination = default_destination

    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        target = destination or self._default_destination
        event_type = str(getattr(event, "event_type", None) or type(event).__name__)
        if is_dataclass(event) and not isinstance(event, type):
            payload: dict[str, Any] = asdict(event)
        else:
            payload = dict(getattr(event, "__dict__", {}))
        try:
            await self._producer.publish(target, event_type, payload)
            _logger.debug("Published event %s to %s", event_type, target)
        except Exception as exc:
            _logger.error("Failed to publish event %s to %s: %s", event_type, target, exc)
            raise
