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
"""Outbound ports: the Spring-parity repository hierarchy and session interface.

The repository protocols mirror Spring Data's reactive lineage, adapted to
asyncio (``async def`` returning materialised values + an ``AsyncIterator``
streaming method as the ``Flux<T>`` analogue):

    CrudRepository[T, ID]
       └─ ReactiveSortingRepository[T, ID]        # + find_all(Sort), stream_all
             └─ PagingAndSortingRepository[T, ID]  # + find_all(Pageable) -> Page[T]

``RepositoryPort`` is retained as the hexagonal "secondary port" name and is an
alias of :class:`CrudRepository` so the framework has a single CRUD vocabulary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort

T = TypeVar("T")
ID = TypeVar("ID")


@runtime_checkable
class CrudRepository(Protocol[T, ID]):
    """Async analogue of Spring Data ``ReactiveCrudRepository``.

    Type Parameters:
        T: The entity/document type.
        ID: The primary-key type (e.g. ``UUID``, ``int``, ``str``).
    """

    async def save(self, entity: T) -> T: ...

    async def save_all(self, entities: list[T]) -> list[T]: ...

    async def find_by_id(self, id: ID) -> T | None: ...

    async def find_all(self, **filters: Any) -> list[T]: ...

    async def find_all_by_id(self, ids: list[ID]) -> list[T]: ...

    async def exists_by_id(self, id: ID) -> bool: ...

    async def count(self) -> int: ...

    async def delete(self, entity: T) -> None: ...

    async def delete_by_id(self, id: ID) -> None: ...

    async def delete_all_by_id(self, ids: list[ID]) -> None: ...

    async def delete_all(self, entities: list[T] | None = None) -> None: ...


@runtime_checkable
class ReactiveSortingRepository(CrudRepository[T, ID], Protocol[T, ID]):
    """Adds sorted fetch-all and Flux-style streaming (``ReactiveSortingRepository``)."""

    @overload
    async def find_all(self, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...

    def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]: ...


@runtime_checkable
class PagingAndSortingRepository(ReactiveSortingRepository[T, ID], Protocol[T, ID]):
    """Adds Page-returning fetch (``PagingAndSortingRepository.findAll(Pageable)``)."""

    @overload
    async def find_all(self, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, **filters: Any) -> Page[T]: ...


# Backwards-compatible hexagonal alias — the generic outbound CRUD port now
# shares the Spring-parity contract (one CRUD vocabulary across the framework).
RepositoryPort = CrudRepository


@runtime_checkable
class SessionPort(Protocol):
    """Abstract session interface for transaction management."""

    async def begin(self) -> Any: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
