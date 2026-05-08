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
"""DDD :class:`Entity` — identity-based equality.

An *entity* is an object whose identity is more important than its
attribute values. Two entities are equal if and only if they share the
same identifier, regardless of any other state.

This base class is intentionally tiny and dependency-free. It is the
non-event-sourced counterpart to :class:`pyfly.eventsourcing.AggregateRoot`,
which adds an append-only event log on top of the same identity model.
"""

from __future__ import annotations

from typing import Generic, TypeVar

TID = TypeVar("TID")


class Entity(Generic[TID]):
    """Base class for all DDD entities.

    Subclasses set ``id`` either through ``__init__`` or by assigning
    the attribute later. Entities with ``id is None`` are treated as
    *transient* (newly created, not yet persisted) and compare equal
    only by Python identity, never by id.
    """

    __slots__ = ("id",)

    def __init__(self, id: TID | None = None) -> None:
        self.id: TID | None = id

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if not isinstance(other, Entity):
            return NotImplemented
        if type(self) is not type(other):
            return NotImplemented
        if self.id is None or other.id is None:
            return False
        return self.id == other.id

    def __hash__(self) -> int:
        if self.id is None:
            return object.__hash__(self)
        return hash((type(self).__name__, self.id))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self.id!r})"

    @property
    def is_transient(self) -> bool:
        """``True`` while this entity has no identifier assigned."""
        return self.id is None
