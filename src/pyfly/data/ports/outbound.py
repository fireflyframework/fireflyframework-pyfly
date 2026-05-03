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
"""Outbound ports: repository and session interfaces."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar, runtime_checkable

from pyfly.data.page import Page

T = TypeVar("T")
ID = TypeVar("ID")


@runtime_checkable
class RepositoryPort(Protocol[T, ID]):
    """Abstract repository interface for CRUD operations.

    Type Parameters:
        T: The entity type.
        ID: The primary key type (e.g. UUID, int, str).
    """

    async def save(self, entity: T) -> T: ...

    async def find_by_id(self, id: ID) -> T | None: ...

    async def find_all(self, **filters: Any) -> list[T]: ...

    async def delete(self, id: ID) -> None: ...

    async def count(self) -> int: ...

    async def exists(self, id: ID) -> bool: ...

    async def save_all(self, entities: list[T]) -> list[T]: ...

    async def find_all_by_ids(self, ids: list[ID]) -> list[T]: ...

    async def delete_all(self, ids: list[ID]) -> int: ...

    async def delete_all_entities(self, entities: list[T]) -> int: ...


@runtime_checkable
class SessionPort(Protocol):
    """Abstract session interface for transaction management."""

    async def begin(self) -> Any: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class CrudRepository(Protocol[T, ID]):
    """Spring Data-style CRUD repository interface."""

    async def save(self, entity: T) -> T: ...

    async def find_by_id(self, id: ID) -> T | None: ...

    async def find_all(self) -> list[T]: ...

    async def delete(self, entity: T) -> None: ...

    async def delete_by_id(self, id: ID) -> None: ...

    async def count(self) -> int: ...

    async def exists_by_id(self, id: ID) -> bool: ...

    async def save_all(self, entities: list[T]) -> list[T]: ...

    async def find_all_by_ids(self, ids: list[ID]) -> list[T]: ...

    async def delete_all(self, ids: list[ID]) -> int: ...

    async def delete_all_entities(self, entities: list[T]) -> int: ...


@runtime_checkable
class PagingRepository(CrudRepository[T, ID], Protocol[T, ID]):
    """CrudRepository with pagination support."""

    async def find_paginated(self, page: int = 1, size: int = 20, sort: list[str] | None = None) -> Page[T]: ...
