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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pyfly.domain.domain_event import DomainEvent, event_payload

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
    * ``payload`` is the event's JSON form: :meth:`pyfly.domain.DomainEvent.to_payload` (instants in
      ISO-8601 UTC), or the same conversion of a dataclass's fields (else ``__dict__``) for any other event
      (:func:`pyfly.domain.domain_event.event_payload`), so every bus can serialize it.

    :attr:`joins_transactions` is the producer's: an outbox bus writes the event in the caller's unit of
    work, and the command bus then publishes inside that unit instead of after its commit. That unit must still
    be open, on the outbox's datasource: a handler whose own ``@transactional`` unit committed before the bus
    publishes has its events written afterwards, in a unit of their own.
    """

    def __init__(self, producer: EventPublisher, default_destination: str = "cqrs.events") -> None:
        self._producer = producer
        self._default_destination = default_destination

    @property
    def joins_transactions(self) -> bool:
        """Whether a publish joins the caller's unit of work (the producer is an outbox bus)."""
        return bool(getattr(self._producer, "joins_transactions", False))

    async def publish(self, event: Any, *, destination: str | None = None) -> None:
        target = destination or self._default_destination
        event_type = str(getattr(event, "event_type", None) or type(event).__name__)
        payload: dict[str, Any] = event.to_payload() if isinstance(event, DomainEvent) else event_payload(event)
        try:
            await self._producer.publish(target, event_type, payload)
            _logger.debug("Published event %s to %s", event_type, target)
        except Exception as exc:
            _logger.error("Failed to publish event %s to %s: %s", event_type, target, exc)
            raise
