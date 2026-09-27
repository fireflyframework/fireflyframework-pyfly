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
"""DDD :class:`DomainEvent` — something that happened in the domain.

This is the *non-event-sourced* counterpart to
:class:`pyfly.eventsourcing.DomainEvent`. The two are deliberately
different: the event-sourcing variant carries an ``event_type`` string
and is replayed to rebuild aggregate state, while this variant is a
plain immutable record collected by an aggregate during a unit of work
and published when that unit commits (see :mod:`pyfly.eda.domain_events`).

Subclasses must be ``@dataclass(frozen=True)``. The base class assigns a
UUID and a UTC timestamp at construction time so every event is
self-identifying.

Every bus carries an event as JSON: :meth:`DomainEvent.to_payload` gives its fields as JSON values (an
instant as ISO-8601 in UTC, a ``Decimal`` or ``UUID`` as a string, an ``Enum`` as its value, a nested
dataclass as an object) and :meth:`DomainEvent.from_payload` reads such a payload back into the typed
event. :func:`event_payload` does the same for any event object, and :func:`to_json_value` and
:func:`from_json_value` convert single values.
"""

from __future__ import annotations

import dataclasses
import enum
import sys
import types
import typing
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Self


@dataclass(frozen=True)
class DomainEvent:
    """Base for transient domain events raised by aggregates."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def event_type(self) -> str:
        """Logical event type — defaults to the subclass name."""
        return type(self).__name__

    def to_payload(self) -> dict[str, Any]:
        """The event's fields as JSON values (see the module documentation); raises ``TypeError`` naming a
        field whose value JSON cannot carry."""
        return event_payload(self)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """The event *payload* (from :meth:`to_payload`, possibly through JSON) describes.

        Each field is converted to its annotated type; a field the payload lacks takes its default (a new
        ``event_id``, the current ``occurred_at``), and a key no field declares is ignored, so a payload of an
        earlier or a later version of the event still reads.
        """
        hints = typing.get_type_hints(cls, globalns=vars(sys.modules[cls.__module__]))
        values = {
            item.name: from_json_value(payload[item.name], hints.get(item.name, Any))
            for item in dataclasses.fields(cls)
            if item.init and item.name in payload
        }
        return cls(**values)


def event_payload(event: object) -> dict[str, Any]:
    """The JSON payload of any event object: a dataclass's fields, or else its ``__dict__``, as JSON values."""
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        return {item.name: _field_value(event, item.name) for item in dataclasses.fields(event)}
    return {name: _field_value(event, name) for name in vars(event) if not name.startswith("_")}


def _field_value(event: object, name: str) -> Any:
    try:
        return to_json_value(getattr(event, name))
    except TypeError as error:
        raise TypeError(f"{type(event).__name__}.{name}: {error}") from error


def to_json_value(value: Any) -> Any:
    """*value* as a JSON value: instants in ISO-8601 UTC (a naive one taken as UTC), dates and times in
    ISO-8601, ``UUID`` and ``Decimal`` as strings, an ``Enum`` as its value, a dataclass or a mapping as an
    object, any other collection as an array. Raises ``TypeError`` for anything else."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return instant.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return to_json_value(value.value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: to_json_value(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json_value(item) for item in value]
    raise TypeError(f"a {type(value).__name__} value has no JSON form")


def from_json_value(value: Any, annotation: Any) -> Any:
    """*value* (a JSON value) converted to *annotation*: the inverse of :func:`to_json_value` for the types it
    writes, ``Optional``/union members tried in order, and anything unannotated (``Any``) left as it is."""
    if value is None or annotation is Any:
        return value
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        members = [member for member in typing.get_args(annotation) if member is not type(None)]
        for member in members:
            try:
                return from_json_value(value, member)
            except (TypeError, ValueError):
                continue
        return value
    if origin in (list, set, frozenset, tuple) or annotation in (list, set, frozenset, tuple):
        return _collection(value, origin or annotation, typing.get_args(annotation))
    if origin in (dict, Mapping) or annotation is dict:
        arguments = typing.get_args(annotation)
        item_type = arguments[1] if len(arguments) == 2 else Any
        return {key: from_json_value(item, item_type) for key, item in dict(value).items()}
    if not isinstance(annotation, type):
        return value
    return _scalar(value, annotation)


def _collection(value: Any, kind: Any, arguments: tuple[Any, ...]) -> Any:
    items = list(value)
    if kind is tuple:
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            return tuple(from_json_value(item, arguments[0]) for item in items)
        if arguments:
            return tuple(from_json_value(item, argument) for item, argument in zip(items, arguments, strict=False))
        return tuple(items)
    item_type = arguments[0] if arguments else Any
    converted = [from_json_value(item, item_type) for item in items]
    return kind(converted) if kind in (set, frozenset) else converted


def _scalar(value: Any, annotation: type) -> Any:
    if isinstance(value, annotation) and not issubclass(annotation, datetime | date):
        return value
    if issubclass(annotation, datetime):
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return instant.replace(tzinfo=UTC) if instant.tzinfo is None else instant
    if issubclass(annotation, date):
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    if issubclass(annotation, time):
        return time.fromisoformat(str(value))
    if issubclass(annotation, uuid.UUID):
        return uuid.UUID(str(value))
    if issubclass(annotation, Decimal):
        return Decimal(str(value))
    if issubclass(annotation, enum.Enum):
        return annotation(value)
    if dataclasses.is_dataclass(annotation) and isinstance(value, Mapping):
        hints = typing.get_type_hints(annotation, globalns=vars(sys.modules[annotation.__module__]))
        return annotation(
            **{
                item.name: from_json_value(value[item.name], hints.get(item.name, Any))
                for item in dataclasses.fields(annotation)
                if item.init and item.name in value
            }
        )
    if annotation in (int, float, str, bool):
        return annotation(value)
    return value
