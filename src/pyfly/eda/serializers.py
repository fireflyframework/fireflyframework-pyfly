# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Pluggable event serializers — JSON (built-in) plus stubs for Avro / Protobuf."""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope


@runtime_checkable
class EventSerializer(Protocol):
    name: str

    def serialize(self, envelope: EventEnvelope) -> bytes: ...
    def deserialize(self, data: bytes) -> EventEnvelope: ...


class JsonEventSerializer:
    name = "json"

    def serialize(self, envelope: EventEnvelope) -> bytes:
        return json.dumps(
            {
                "event_id": envelope.event_id,
                "event_type": envelope.event_type,
                "payload": envelope.payload,
                "destination": envelope.destination,
                "timestamp": envelope.timestamp.isoformat(),
                "headers": envelope.headers,
            }
        ).encode("utf-8")

    def deserialize(self, data: bytes) -> EventEnvelope:
        from datetime import datetime

        raw = json.loads(data.decode("utf-8"))
        return EventEnvelope(
            event_id=raw["event_id"],
            event_type=raw["event_type"],
            payload=raw["payload"],
            destination=raw["destination"],
            timestamp=datetime.fromisoformat(raw["timestamp"]),
            headers=raw.get("headers", {}),
        )


class AvroEventSerializer:
    """Stub Avro serializer — wire up your Schema Registry adapter to enable."""

    name = "avro"

    def __init__(self, schema_registry: object | None = None) -> None:
        self._registry = schema_registry

    def serialize(self, envelope: EventEnvelope) -> bytes:
        msg = "Avro serializer requires a Schema Registry adapter"
        raise NotImplementedError(msg)

    def deserialize(self, data: bytes) -> EventEnvelope:
        msg = "Avro serializer requires a Schema Registry adapter"
        raise NotImplementedError(msg)


class ProtobufEventSerializer:
    """Stub Protobuf serializer — bring your own descriptor."""

    name = "protobuf"

    def serialize(self, envelope: EventEnvelope) -> bytes:
        msg = "Protobuf serializer requires a registered message type"
        raise NotImplementedError(msg)

    def deserialize(self, data: bytes) -> EventEnvelope:
        msg = "Protobuf serializer requires a registered message type"
        raise NotImplementedError(msg)
