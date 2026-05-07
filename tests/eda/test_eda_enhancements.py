# Copyright 2026 Firefly Software Foundation.
# Licensed under the Apache License, Version 2.0.
"""Tests for the new EDA enhancements (DLQ, circuit breaker, serializers, filters)."""

from __future__ import annotations

import pytest

from pyfly.eda.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitOpenError,
    EventCircuitBreaker,
)
from pyfly.eda.dlq import EdaDeadLetterEntry, InMemoryEdaDeadLetterStore
from pyfly.eda.filter import HeaderEventFilter, PredicateEventFilter
from pyfly.eda.serializers import (
    AvroEventSerializer,
    JsonEventSerializer,
    ProtobufEventSerializer,
)
from pyfly.eda.types import EventEnvelope


def _envelope(headers: dict[str, str] | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_type="OrderPlaced",
        payload={"id": 1},
        destination="orders.events",
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_dlq_round_trip() -> None:
    store = InMemoryEdaDeadLetterStore()
    entry = EdaDeadLetterEntry(event=_envelope(), error_type="X", error_message="boom")
    await store.add(entry)
    listed = await store.list()
    assert len(listed) == 1
    assert listed[0].event.event_type == "OrderPlaced"
    assert await store.delete(entry.id)


@pytest.mark.asyncio
async def test_circuit_breaker_opens() -> None:
    cb = EventCircuitBreaker(CircuitBreakerConfig(failure_threshold=2))

    async def failing() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.execute(failing)
    with pytest.raises(CircuitOpenError):
        await cb.execute(failing)


def test_json_serializer_round_trip() -> None:
    s = JsonEventSerializer()
    envelope = _envelope({"x": "y"})
    raw = s.serialize(envelope)
    restored = s.deserialize(raw)
    assert restored.event_type == envelope.event_type
    assert restored.payload == envelope.payload


def test_avro_protobuf_serializers_raise_until_configured() -> None:
    with pytest.raises(NotImplementedError):
        AvroEventSerializer().serialize(_envelope())
    with pytest.raises(NotImplementedError):
        ProtobufEventSerializer().serialize(_envelope())


def test_header_filter() -> None:
    f = HeaderEventFilter("x-tenant", r"^acme-.+$")
    assert f.accepts(_envelope({"x-tenant": "acme-eu"}))
    assert not f.accepts(_envelope({"x-tenant": "other"}))


def test_predicate_filter() -> None:
    f = PredicateEventFilter(lambda e: e.event_type.startswith("Order"))
    assert f.accepts(_envelope())
    f2 = PredicateEventFilter(lambda e: e.event_type == "X")
    assert not f2.accepts(_envelope())
