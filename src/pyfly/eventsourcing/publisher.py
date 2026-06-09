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
"""EDA bridge: forwards stored-event envelopes onto the event bus."""

from __future__ import annotations

from collections.abc import Iterable

from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eventsourcing.event import StoredEventEnvelope


class EventSourcingPublisher:
    """Bridges :class:`StoredEventEnvelope` instances onto the EDA bus.

    Ctor args:
        event_publisher: the EDA :class:`EventPublisher` to forward events to.
        destination:     default routing destination (topic / exchange / subject).
    """

    def __init__(self, event_publisher: EventPublisher, *, destination: str = "pyfly.events") -> None:
        self._publisher = event_publisher
        self._destination = destination

    async def publish(self, envelope: StoredEventEnvelope) -> None:
        """Publish a single stored-event envelope onto the EDA bus."""
        headers: dict[str, str] = {
            "aggregate_id": envelope.aggregate_id,
            "aggregate_type": envelope.aggregate_type,
            "sequence": str(envelope.sequence),
            "version": str(envelope.version),
        }
        if envelope.tenant_id is not None:
            headers["tenant_id"] = envelope.tenant_id
        # Merge any string-valued metadata entries as headers.
        for key, value in envelope.metadata.items():
            if isinstance(value, str):
                headers[key] = value
        await self._publisher.publish(
            self._destination,
            event_type=envelope.event_type,
            payload=envelope.payload,
            headers=headers,
        )

    async def publish_all(self, envelopes: Iterable[StoredEventEnvelope]) -> None:
        """Publish every envelope in *envelopes* sequentially."""
        for envelope in envelopes:
            await self.publish(envelope)
