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
"""Outbound ports: the Spring-parity repository hierarchy and the (deprecated) session port.

The repository protocols mirror Spring Data's reactive lineage, adapted to
asyncio (``async def`` returning materialised values + an ``AsyncIterator``
streaming method as the ``Flux<T>`` analogue):

    CrudRepository[T, ID]
       └─ ReactiveSortingRepository[T, ID]        # + find_all(Sort), stream_all
             └─ PagingAndSortingRepository[T, ID]  # + find_all(Pageable) -> Page[T]
                   └─ BatchRepository[T, ID]       # + delete_*_in_batch, find_slice(Pageable) -> Slice[T]

``RepositoryPort`` is retained as the hexagonal "secondary port" name and is an
alias of :class:`CrudRepository` so the framework has a single CRUD vocabulary.

:class:`Persistable` is the optional hook an entity implements to tell ``save`` whether it is new (Spring's
``Persistable.isNew()``): an entity with an application-assigned key that knows it was never stored is then
inserted at once, without the lookup a merge needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, TypeVar, overload, runtime_checkable

from pyfly.data.page import Page, Slice
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.transaction.manager import TransactionManager

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


@runtime_checkable
class BatchRepository(PagingAndSortingRepository[T, ID], Protocol[T, ID]):
    """Adds bulk deletes and count-free paging (Spring ``JpaRepository``'s ``deleteAllInBatch`` and
    ``deleteAllByIdInBatch``, and ``Slice``).

    ``delete_all``/``delete_all_by_id`` delete entity by entity, so the backend's cascades, version checks and
    delete hooks run; the ``*_in_batch`` forms are one bulk statement per chunk that bypasses them, by design.
    ``find_slice`` returns a page and whether another one follows, with no count query.
    """

    async def delete_all_in_batch(self, entities: list[T] | None = None) -> None: ...

    async def delete_all_by_id_in_batch(self, ids: list[ID]) -> None: ...

    async def find_slice(self, pageable: Pageable, **filters: Any) -> Slice[T]: ...


@runtime_checkable
class Persistable(Protocol):
    """An entity that tells ``save`` whether it is new (Spring's ``Persistable``).

    Without it, an entity is new when its version is ``None`` (a versioned entity), else when its primary key
    is ``None``; an entity that is not new is merged (a lookup, then an ``UPDATE`` or an ``INSERT``). Implement
    ``is_new`` as a method (or a property) on the entity class; a mapped column of that name is not a hook.
    """

    def is_new(self) -> bool: ...


# Backwards-compatible hexagonal alias — the generic outbound CRUD port now
# shares the Spring-parity contract (one CRUD vocabulary across the framework).
RepositoryPort = CrudRepository


# Deprecated: nothing implemented the old three-method SessionPort. Transactions are driven through the
# TransactionManager SPI of the unit of work (begin/commit/rollback per datasource, savepoints, auto units),
# which every backend adapter implements.
SessionPort = TransactionManager
"""Deprecated alias of :class:`pyfly.data.transaction.manager.TransactionManager`."""
