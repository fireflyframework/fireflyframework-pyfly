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
"""Repository that performs soft deletes instead of hard deletes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, TypeVar, overload

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.relational.sqlalchemy.repository import ID, Repository
from pyfly.data.relational.sqlalchemy.specification import Specification

T = TypeVar("T")


class SoftDeleteRepository(Repository[T, ID]):
    """Repository that performs soft deletes instead of hard deletes.

    Entities must use :class:`SoftDeleteMixin` to have a ``deleted_at`` column.
    All find methods automatically exclude soft-deleted entities.
    """

    @property
    def _active(self) -> Any:
        return self._model.deleted_at == None  # type: ignore[attr-defined]  # noqa: E711

    def _active_select(self, **filters: Any) -> Any:
        stmt = select(self._model).where(self._active)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        return stmt

    # ------------------------------------------------------------------
    # Soft-delete writes
    # ------------------------------------------------------------------

    async def delete(self, entity: T) -> None:
        """Soft-delete a managed entity by stamping ``deleted_at``."""
        entity.deleted_at = datetime.now(UTC)  # type: ignore[attr-defined]
        await self._require_session().flush()

    async def delete_by_id(self, id: ID) -> None:
        """Soft-delete by id: set ``deleted_at`` instead of removing from DB."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None:
            entity.deleted_at = datetime.now(UTC)  # type: ignore[attr-defined]
            await session.flush()

    async def delete_all_by_id(self, ids: list[ID]) -> None:
        """Soft-delete all entities with given IDs."""
        if not ids:
            return
        session = self._require_session()
        await session.execute(
            sa_update(self._model).where(self._pk_column.in_(ids)).values(deleted_at=datetime.now(UTC))
        )
        await session.flush()

    async def delete_all(self, entities: list[T] | None = None) -> None:
        """Soft-delete the given entities, or ALL active rows when ``entities`` is ``None``."""
        session = self._require_session()
        now = datetime.now(UTC)
        if entities is None:
            await session.execute(sa_update(self._model).where(self._active).values(deleted_at=now))
        else:
            for entity in entities:
                entity.deleted_at = now  # type: ignore[attr-defined]
        await session.flush()

    async def hard_delete(self, id: ID) -> None:
        """Permanently delete an entity (bypass soft delete)."""
        await super().delete_by_id(id)

    async def restore(self, id: ID) -> T | None:
        """Restore a soft-deleted entity by clearing ``deleted_at``."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None and hasattr(entity, "deleted_at"):
            entity.deleted_at = None
            await session.flush()
            await session.refresh(entity)
            return entity
        return None

    # ------------------------------------------------------------------
    # Reads — every path excludes soft-deleted rows (audit #103)
    # ------------------------------------------------------------------

    async def find_by_id(self, id: ID) -> T | None:
        """Find by ID, excluding soft-deleted entities."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None and hasattr(entity, "deleted_at") and entity.deleted_at is not None:
            return None
        return entity

    async def exists_by_id(self, id: ID) -> bool:
        """Check existence, excluding soft-deleted entities."""
        return await self.find_by_id(id) is not None

    @overload
    async def find_all(self, criteria: None = ..., **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Sort, **filters: Any) -> list[T]: ...
    @overload
    async def find_all(self, criteria: Pageable, **filters: Any) -> Page[T]: ...
    async def find_all(self, criteria: Sort | Pageable | None = None, **filters: Any) -> list[T] | Page[T]:
        """Spring ``findAll`` family, excluding soft-deleted rows."""
        session = self._require_session()
        if isinstance(criteria, Pageable):
            base = self._active_select(**filters)
            total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
            stmt = self._apply_orders(base, criteria.sort).offset(criteria.offset).limit(criteria.size)
            items = list((await session.execute(stmt)).scalars().all())
            return Page(items=items, total=total, page=criteria.page, size=criteria.size)
        stmt = self._active_select(**filters)
        if isinstance(criteria, Sort):
            stmt = self._apply_orders(stmt, criteria)
        return list((await session.execute(stmt)).scalars().all())

    async def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]:
        """Stream non-deleted entities lazily."""
        stmt = self._active_select(**filters)
        if criteria is not None:
            stmt = self._apply_orders(stmt, criteria)
        result = await self._require_session().stream_scalars(stmt)
        async for row in result:
            yield row

    async def find_all_including_deleted(self, **filters: Any) -> list[T]:
        """Find all entities INCLUDING soft-deleted ones."""
        session = self._require_session()
        stmt = self._filtered_select(**filters)
        return list((await session.execute(stmt)).scalars().all())

    async def find_all_by_id(self, ids: list[ID]) -> list[T]:
        if not ids:
            return []
        session = self._require_session()
        stmt = select(self._model).where(self._pk_column.in_(ids)).where(self._active)
        return list((await session.execute(stmt)).scalars().all())

    async def count(self) -> int:
        """Count non-deleted entities."""
        session = self._require_session()
        stmt = select(func.count()).select_from(self._model).where(self._active)
        result = await session.execute(stmt)
        return result.scalar_one()

    async def find_all_by_spec(self, spec: Specification[T]) -> list[T]:
        session = self._require_session()
        stmt = select(self._model).where(self._active)
        stmt = spec.to_predicate(self._model, stmt)
        return list((await session.execute(stmt)).scalars().all())

    async def find_all_by_spec_paged(self, spec: Specification[T], pageable: Pageable) -> Page[T]:
        session = self._require_session()
        filtered = select(self._model).where(self._active)
        filtered = spec.to_predicate(self._model, filtered)
        total = (await session.execute(select(func.count()).select_from(filtered.subquery()))).scalar_one()
        stmt = self._apply_orders(filtered, pageable.sort).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)
