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
"""Domain :class:`DomainRepository` — collection-like aggregate store.

This is a *DDD* repository: it speaks in aggregates, not rows. The
contract is intentionally small (``add``, ``find``, ``remove``, ``next_id``)
because aggregate boundaries are supposed to make complex queries
unnecessary inside the domain layer. Read-side queries belong in
projections and CQRS query handlers.

Concrete implementations live wherever they are needed — typically a
SQLAlchemy adapter alongside the rest of the infra layer. The protocol
is structural so any class with the right method signatures satisfies
it; subclassing is optional.
"""

from __future__ import annotations

from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pyfly.domain.aggregate_root import AggregateRoot

A = TypeVar("A", bound=AggregateRoot[Any])
TID = TypeVar("TID")


@runtime_checkable
class DomainRepository(Protocol, Generic[A, TID]):
    """Collection-like store for aggregates of type ``A``."""

    async def add(self, aggregate: A) -> A: ...

    async def find(self, id: TID) -> A | None: ...

    async def remove(self, aggregate: A) -> None: ...

    async def next_id(self) -> TID: ...
