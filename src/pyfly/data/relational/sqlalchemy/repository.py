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
"""Generic async repository built on SQLAlchemy 2.0."""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast, get_args, get_origin

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable
from pyfly.data.relational.sqlalchemy.specification import Specification

T = TypeVar("T")
ID = TypeVar("ID")


class Repository(Generic[T, ID]):
    """Generic CRUD repository for SQLAlchemy entities.

    Provides standard data access operations with async support.
    Subclass with concrete type parameters to enable DI-managed repositories.

    Type Parameters:
        T: The entity type (any SQLAlchemy model).
        ID: The primary key type (e.g. UUID, int, str).

    Usage::

        class UserRepository(Repository[User, UUID]):
            pass  # entity type auto-extracted, session injected by DI
    """

    _entity_type: type | None = None
    _id_type: type | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is Repository:
                args = get_args(base)
                if args and not isinstance(args[0], TypeVar):
                    cls._entity_type = args[0]
                if len(args) > 1 and not isinstance(args[1], TypeVar):
                    cls._id_type = args[1]
                break

    def __init__(self, model: type[T] | None = None, session: AsyncSession | None = None) -> None:
        resolved = model or getattr(type(self), "_entity_type", None)
        if resolved is None:
            raise TypeError(
                f"{type(self).__name__} requires either Repository[Entity, ID] declaration or explicit model argument"
            )
        self._model: type[T] = cast(type[T], resolved)
        self._session = session

    def _require_session(self) -> AsyncSession:
        """Return the session or raise if none is configured."""
        if self._session is None:
            raise RuntimeError("No AsyncSession configured — ensure a session bean is registered")
        return self._session

    @property
    def _pk_column(self) -> Any:
        """Discover the primary key column dynamically."""
        from sqlalchemy import inspect as sa_inspect

        mapper = sa_inspect(self._model)
        if mapper is None:
            return self._model.id  # type: ignore[attr-defined]
        pk_cols = mapper.primary_key
        if pk_cols:
            return getattr(self._model, pk_cols[0].name)
        return self._model.id  # type: ignore[attr-defined]

    def _apply_sort(self, stmt: Select[Any], pageable: Pageable) -> Select[Any]:
        """Apply sort orders from a Pageable to a SELECT statement."""
        for order in pageable.sort.orders:
            col = getattr(self._model, order.property)
            stmt = stmt.order_by(col.asc() if order.direction == "asc" else col.desc())
        return stmt

    async def save(self, entity: T) -> T:
        """Persist an entity (insert or update)."""
        session = self._require_session()
        session.add(entity)
        await session.flush()
        await session.refresh(entity)
        return entity

    async def find_by_id(self, id: ID) -> T | None:
        """Find an entity by its primary key."""
        session = self._require_session()
        return await session.get(self._model, id)

    async def find_all(self, **filters: Any) -> list[T]:
        """Find all entities, optionally filtered by column values."""
        session = self._require_session()
        stmt = select(self._model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete(self, id: ID) -> None:
        """Delete an entity by its primary key."""
        session = self._require_session()
        entity = await self.find_by_id(id)
        if entity is not None:
            await session.delete(entity)
            await session.flush()

    async def find_paginated(self, page: int = 1, size: int = 20, pageable: Pageable | None = None) -> Page[T]:
        """Find entities with pagination.

        Args:
            page: Page number (1-based).
            size: Number of items per page.
            pageable: Optional Pageable with page, size, and sort criteria.
                      When provided, its page/size/sort override the primitives.

        Returns:
            A Page[T] containing the results and pagination metadata.
        """
        session = self._require_session()
        if pageable is not None:
            page = pageable.page
            size = pageable.size

        # Count total
        count_stmt = select(func.count()).select_from(self._model)
        total_result = await session.execute(count_stmt)
        total = total_result.scalar_one()

        # Fetch page
        offset = (page - 1) * size
        stmt = select(self._model)

        # Apply sorting from pageable
        if pageable is not None:
            stmt = self._apply_sort(stmt, pageable)

        stmt = stmt.offset(offset).limit(size)
        result = await session.execute(stmt)
        items = list(result.scalars().all())

        return Page(items=items, total=total, page=page, size=size)

    async def find_all_by_spec(self, spec: Specification[T]) -> list[T]:
        """Find all entities matching the specification."""
        session = self._require_session()
        stmt = select(self._model)
        stmt = spec.to_predicate(self._model, stmt)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def find_all_by_spec_paged(self, spec: Specification[T], pageable: Pageable) -> Page[T]:
        """Find entities matching the specification with pagination and sorting."""
        session = self._require_session()
        # Count with spec
        base = select(self._model)
        filtered = spec.to_predicate(self._model, base)
        count_stmt = select(func.count()).select_from(filtered.subquery())
        total_result = await session.execute(count_stmt)
        total = total_result.scalar_one()

        # Apply sorting from Pageable.sort
        stmt = self._apply_sort(filtered, pageable)

        # Apply pagination
        stmt = stmt.offset(pageable.offset).limit(pageable.size)
        result = await session.execute(stmt)
        items = list(result.scalars().all())

        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    async def count(self) -> int:
        """Return the total number of entities."""
        session = self._require_session()
        stmt = select(func.count()).select_from(self._model)
        result = await session.execute(stmt)
        return result.scalar_one()

    async def exists(self, id: ID) -> bool:
        """Check if an entity with the given ID exists."""
        entity = await self.find_by_id(id)
        return entity is not None

    async def save_all(self, entities: list[T]) -> list[T]:
        """Persist multiple entities in a single batch."""
        session = self._require_session()
        session.add_all(entities)
        await session.flush()
        for entity in entities:
            await session.refresh(entity)
        return entities

    async def find_all_by_ids(self, ids: list[ID]) -> list[T]:
        """Find all entities with IDs in the given list."""
        if not ids:
            return []
        session = self._require_session()
        stmt = select(self._model).where(self._pk_column.in_(ids))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def delete_all(self, ids: list[ID]) -> int:
        """Delete all entities with IDs in the given list. Returns count deleted."""
        if not ids:
            return 0
        session = self._require_session()
        from sqlalchemy import delete as sa_delete

        stmt = sa_delete(self._model).where(self._pk_column.in_(ids))
        result = await session.execute(stmt)
        await session.flush()
        return cast(int, result.rowcount)  # type: ignore[attr-defined]

    async def delete_all_entities(self, entities: list[T]) -> int:
        """Delete all given entity instances. Returns count deleted."""
        session = self._require_session()
        count = 0
        for entity in entities:
            await session.delete(entity)
            count += 1
        await session.flush()
        return count
