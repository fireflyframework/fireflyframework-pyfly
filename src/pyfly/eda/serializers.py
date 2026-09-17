# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Pluggable event serializers — JSON in both of the family's shapes, plus stubs for Avro / Protobuf."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pyfly.eda.types import EventEnvelope


@runtime_checkable
class EventSerializer(Protocol):
    name: str

    def serialize(self, envelope: EventEnvelope) -> bytes: ...
    def deserialize(self, data: bytes) -> EventEnvelope: ...


class EnvelopeDecodeError(ValueError):
    """A record on the wire is not an event envelope.

    One typed error for every way a body can be wrong — bytes that are not UTF-8 JSON, a JSON
    value that is not an object, a missing event type, a payload that is not an object, a
    timestamp that is not ISO 8601 — so a consumer can dead-letter on one exception type and
    name the reason, instead of catching ``KeyError``/``TypeError``/``ValueError`` one by one.
    """


#: Snake_case is what :class:`JsonEventSerializer` writes; camelCase is what the LaraFly
#: (PHP) implementation of the family writes (``EventEnvelope::toArray()``). Both are read.
_EVENT_TYPE_KEYS = ("event_type", "eventType")
_EVENT_ID_KEYS = ("event_id", "eventId")


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _as_mapping(value: Any, name: str) -> dict[str, Any]:
    """Coerce PHP's empty-array quirk to a dict.

    ``json_encode([])`` in PHP produces ``[]`` (a JSON *array*), not ``{}``, so an envelope
    published by LaraFly with no headers arrives as a Python ``list`` — and any later
    ``headers.get(...)`` raises ``AttributeError`` deep inside a handler. Only the empty list is
    tolerated; a non-empty list is a genuine contract violation.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and not value:
        return {}
    msg = f"envelope field {name!r} must be a JSON object, got {type(value).__name__}"
    raise EnvelopeDecodeError(msg)


def _parse_timestamp(value: Any) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating both sides' spellings.

    LaraFly emits PHP ``DATE_ATOM`` (``2026-09-05T10:00:00+00:00``); Python's ``isoformat()``
    may add microseconds and may use ``Z``. ``fromisoformat`` on CPython >= 3.11 accepts all
    three. A missing timestamp is stamped now; a naive one is taken as UTC, which is what the
    writer meant, since every serializer in the family writes UTC.
    """
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, str):
        msg = f"envelope field 'timestamp' must be an ISO 8601 string, got {type(value).__name__}"
        raise EnvelopeDecodeError(msg)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"envelope field 'timestamp' is not ISO 8601: {value!r}"
        raise EnvelopeDecodeError(msg) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def decode_envelope(data: bytes) -> EventEnvelope:
    """Read an envelope in either of the family's JSON shapes.

    PyFly writes ``{"event_id", "event_type", "payload", "destination", "timestamp", "headers"}``;
    LaraFly writes ``{"eventType", "destination", "payload", "headers", "eventId", "timestamp"}``.
    Until 26.09.05 this reader did ``raw["event_id"]`` and a record from the PHP side died with a
    ``KeyError`` — on a topic the two runtimes share, every cross-runtime event was poison. The
    event type and the destination are required; an absent event id or timestamp is filled in,
    because both are metadata the reader can stand in for and a handler needs neither to run.
    """
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"envelope is not valid UTF-8 JSON: {exc}"
        raise EnvelopeDecodeError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"envelope must be a JSON object, got {type(raw).__name__}"
        raise EnvelopeDecodeError(msg)

    event_type = _first(raw, _EVENT_TYPE_KEYS)
    if not isinstance(event_type, str) or not event_type:
        msg = "envelope carries no event type (neither 'event_type' nor 'eventType')"
        raise EnvelopeDecodeError(msg)
    destination = raw.get("destination")
    if not isinstance(destination, str):
        msg = "envelope carries no 'destination'"
        raise EnvelopeDecodeError(msg)
    event_id = _first(raw, _EVENT_ID_KEYS)
    if event_id is None:
        event_id = str(uuid.uuid4())
    elif not isinstance(event_id, str):
        msg = f"envelope field 'eventId' must be a string, got {type(event_id).__name__}"
        raise EnvelopeDecodeError(msg)

    headers = _as_mapping(raw.get("headers"), "headers")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        msg = "envelope field 'headers' must map strings to strings"
        raise EnvelopeDecodeError(msg)

    return EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        payload=_as_mapping(raw.get("payload"), "payload"),
        destination=destination,
        timestamp=_parse_timestamp(raw.get("timestamp")),
        headers=headers,
    )


class JsonEventSerializer:
    """The default serializer: writes PyFly's snake_case envelope, reads both family shapes."""

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
        return decode_envelope(data)


class FireflyJsonEventSerializer:
    """Writes the envelope the way the LaraFly (PHP) implementation of the family does.

    Key order ``eventType, destination, payload, headers, eventId, timestamp`` (the order of
    ``EventEnvelope::toArray()``), compact separators like ``json_encode``, and a ``DATE_ATOM``
    timestamp — second precision, explicit numeric offset, never ``Z``. Sub-second precision is
    dropped on purpose: ``DATE_ATOM`` has none, so keeping it would make a PHP -> Python -> PHP
    round trip lossy in one direction only. Select it with
    ``pyfly.eda.serialization-format: firefly-json`` when a Python service shares topics with a
    LaraFly one and the PHP side is the wire contract.
    """

    name = "firefly-json"

    def serialize(self, envelope: EventEnvelope) -> bytes:
        timestamp = envelope.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        document = {
            "eventType": envelope.event_type,
            "destination": envelope.destination,
            "payload": envelope.payload,
            "headers": envelope.headers,
            "eventId": envelope.event_id,
            "timestamp": timestamp.replace(microsecond=0).isoformat(),
        }
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def deserialize(self, data: bytes) -> EventEnvelope:
        return decode_envelope(data)


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
