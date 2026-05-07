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
"""Domain event types and the wire envelope used by the event store."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class DomainEvent:
    """Marker base class.  Subclasses are simple dataclasses carrying the payload."""

    @property
    def event_type(self) -> str:
        return type(self).__name__


def domain_event(cls: type) -> type:
    """Decorate a dataclass to mark it as a :class:`DomainEvent`.  Annotation only."""
    cls.__domain_event__ = True  # type: ignore[attr-defined]
    return cls


@dataclass
class StoredEventEnvelope:
    """Wire format for events persisted to the event store."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    aggregate_id: str = ""
    aggregate_type: str = ""
    sequence: int = 0
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1
    tenant_id: str | None = None

    @classmethod
    def of(
        cls,
        aggregate_id: str,
        aggregate_type: str,
        sequence: int,
        event: DomainEvent,
        *,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> StoredEventEnvelope:
        payload = asdict(event) if is_dataclass(event) else getattr(event, "__dict__", {})
        return cls(
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            sequence=sequence,
            event_type=event.event_type,
            payload=payload,
            metadata=metadata or {},
            tenant_id=tenant_id,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "aggregate_id": self.aggregate_id,
                "aggregate_type": self.aggregate_type,
                "sequence": self.sequence,
                "event_type": self.event_type,
                "payload": self.payload,
                "metadata": self.metadata,
                "occurred_at": self.occurred_at.isoformat(),
                "version": self.version,
                "tenant_id": self.tenant_id,
            },
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str) -> StoredEventEnvelope:
        data = json.loads(raw)
        return cls(
            event_id=data["event_id"],
            aggregate_id=data["aggregate_id"],
            aggregate_type=data["aggregate_type"],
            sequence=data["sequence"],
            event_type=data["event_type"],
            payload=data["payload"],
            metadata=data["metadata"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            version=data["version"],
            tenant_id=data.get("tenant_id"),
        )
