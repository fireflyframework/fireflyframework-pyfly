# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Bounded private JSON facts borrowed by a trusted asynchronous consumer.

This carrier performs no authorization. The composing application captures and
validates its own facts. Persist the snapshot only in a private envelope, and
redact that envelope from public data with ``public_consumer_data``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from typing import cast, overload

from pydantic import JsonValue, TypeAdapter, ValidationError

PRIVATE_CONSUMER_CONTEXT = "__pyfly_consumer_context__"
_MAX_BYTES = 16384
_JSON = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class _Scope:
    encoded: str
    closed: Event
    parent: _Scope | None


_current: ContextVar[_Scope | None] = ContextVar("pyfly_consumer_context", default=None)


@contextmanager
def bind_consumer_context(value: Mapping[str, JsonValue]) -> Iterator[None]:
    try:
        checked = _JSON.validate_python(value, strict=True)
        encoded = json.dumps(checked, allow_nan=False, ensure_ascii=False)
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Consumer context must contain JSON facts") from None
    if len(encoded.encode()) > _MAX_BYTES:
        raise ValueError("Consumer context exceeds 16384 bytes")
    consumer_context()
    scope = _Scope(encoded, Event(), _current.get())
    token = _current.set(scope)
    try:
        yield
    finally:
        scope.closed.set()
        _current.reset(token)


def consumer_context() -> dict[str, JsonValue] | None:
    scope = _current.get()
    current = scope
    while current is not None:
        if current.closed.is_set():
            raise RuntimeError("Consumer context is closed")
        current = current.parent
    return cast(dict[str, JsonValue], json.loads(scope.encoded)) if scope is not None else None


@overload
def public_consumer_data(value: dict[str, JsonValue]) -> dict[str, JsonValue]: ...


@overload
def public_consumer_data(value: list[JsonValue]) -> list[JsonValue]: ...


@overload
def public_consumer_data(value: JsonValue) -> JsonValue: ...


def public_consumer_data(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: public_consumer_data(item) for key, item in value.items() if key != PRIVATE_CONSUMER_CONTEXT}
    if isinstance(value, list):
        return [public_consumer_data(item) for item in value]
    return value
