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

from collections.abc import AsyncIterator
from typing import Any, Generic, TypeVar, cast, get_args, get_origin, overload

from sqlalchemy import Select, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.relational.sqlalchemy.specification import Specification

T = TypeVar("T")
ID = TypeVar("ID")


class Repository(Generic[T, ID]):
    """Generic CRUD repository for SQLAlchemy entities.

    Implements the Spring-parity ``PagingAndSortingRepository`` contract
    (``CrudRepository`` → ``ReactiveSortingRepository`` → paging) with async
    support. Subclass with concrete type parameters to enable DI-managed
    repositories.

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

    def _filtered_select(self, **filters: Any) -> Select[Any]:
        """Build a ``SELECT`` for the model with optional equality filters."""
        stmt = select(self._model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        return stmt

    def _apply_orders(self, stmt: Select[Any], sort: Sort) -> Select[Any]:
        """Apply a :class:`Sort`'s orders to a ``SELECT`` statement."""
        for order in sort.orders:
            col = getattr(self._model, order.property)
            stmt = stmt.order_by(col.asc() if order.direction == "asc" else col.desc())
        return stmt

    # ------------------------------------------------------------------
    # CrudRepository
    # ------------------------------------------------------------------

    async def save(self, entity: T) -> T:
        """Persist an entity (insert or update)."""
        session = self._require_session()
        session.add(entity)
        await session.flush()
        await session.refresh(entity)
        return entity

    async def save_all(self, entities: list[T]) -> list[T]:
        """Persist multiple entities in a single batch."""
        session = self._require_session()
        session.add_all(entities)
        await session.flush()
        for entity in entities:
            await session.refresh(entity)
        return entities

    async def find_by_id(self, id: ID) -> T | None:
        """Find an entity by its primary key."""
        session = self._require_session()
        return await session.get(self._model, id)

    async def find_all_by_id(self, ids: list[ID]) -> list[T]:
        """Find all entities with IDs in the given list (Spring ``findAllById``)."""
        if not ids:
            return []
        session = self._require_session()
        stmt = select(self._model).where(self._pk_column.in_(ids))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def exists_by_id(self, id: ID) -> bool:
        """Check whether an entity with the given id exists (Spring ``existsById``)."""
        return await self.find_by_id(id) is not None

    async def count(self) -> int:
        """Return the total number of entities."""
        session = self._require_session()
        stmt = select(func.count()).select_from(self._model)
        result = await session.execute(stmt)
        return result.scalar_one()

    async def delete(self, entity: T) -> None:
        """Delete a managed entity instance (Spring ``delete(entity)``)."""
        session = self._require_session()
        await session.delete(entity)
        await session.flush()

    async def delete_by_id(self, id: ID) -> None:
        """Delete an entity by its primary key (Spring ``deleteById``)."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None:
            await session.delete(entity)
            await session.flush()

    async def delete_all_by_id(self, ids: list[ID]) -> None:
        """Delete all entities whose ids are in ``ids`` (Spring ``deleteAllById``)."""
        if not ids:
            return
        session = self._require_session()
        await session.execute(sa_delete(self._model).where(self._pk_column.in_(ids)))
        await session.flush()

    async def delete_all(self, entities: list[T] | None = None) -> None:
        """Delete the given entities, or ALL rows when ``entities`` is ``None`` (Spring ``deleteAll``)."""
        session = self._require_session()
        if entities is None:
            await session.execute(sa_delete(self._model))
        else:
            for entity in entities:
                await session.delete(entity)
        await session.flush()

    # ------------------------------------------------------------------
    # ReactiveSortingRepository + PagingAndSortingRepository
    # ------------------------------------------------------------------

    @overload
    async def find_all(self, criteria: None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, **filters: Any) -> Page[T]: ...
    async def find_all(self, criteria: Sort | Pageable | None = None, **filters: Any) -> list[T] | Page[T]:
        """Spring ``findAll`` family.

        - ``find_all()`` / ``find_all(status="X")`` → ``list[T]`` (optionally filtered)
        - ``find_all(Sort.by("name"))`` → sorted ``list[T]``
        - ``find_all(Pageable.of(1, 20))`` → ``Page[T]``
        """
        if isinstance(criteria, Pageable):
            return await self._find_page(criteria, **filters)
        session = self._require_session()
        stmt = self._filtered_select(**filters)
        if isinstance(criteria, Sort):
            stmt = self._apply_orders(stmt, criteria)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]:
        """Stream entities lazily (``Flux<T>`` analogue) via a server-side cursor."""
        session = self._require_session()
        stmt = self._filtered_select(**filters)
        if criteria is not None:
            stmt = self._apply_orders(stmt, criteria)
        result = await session.stream_scalars(stmt)
        async for row in result:
            yield row

    async def _find_page(self, pageable: Pageable, **filters: Any) -> Page[T]:
        session = self._require_session()
        base = self._filtered_select(**filters)
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        stmt = self._apply_orders(base, pageable.sort).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    # ------------------------------------------------------------------
    # Specification extensions (PyFly)
    # ------------------------------------------------------------------

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
        base = select(self._model)
        filtered = spec.to_predicate(self._model, base)
        count_stmt = select(func.count()).select_from(filtered.subquery())
        total = (await session.execute(count_stmt)).scalar_one()
        stmt = self._apply_orders(filtered, pageable.sort).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)
