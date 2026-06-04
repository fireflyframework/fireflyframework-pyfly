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

from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from pyfly.data.relational.sqlalchemy.repository import ID, Repository

T = TypeVar("T")


class SoftDeleteRepository(Repository[T, ID]):
    """Repository that performs soft deletes instead of hard deletes.

    Entities must use :class:`SoftDeleteMixin` to have a ``deleted_at`` column.
    All find methods automatically exclude soft-deleted entities.
    """

    async def delete(self, id: ID) -> None:
        """Soft-delete: set ``deleted_at`` instead of removing from DB."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None:
            entity.deleted_at = datetime.now(UTC)  # type: ignore[attr-defined]
            await session.flush()

    async def find_by_id(self, id: ID) -> T | None:
        """Find by ID, excluding soft-deleted entities."""
        session = self._require_session()
        entity = await session.get(self._model, id)
        if entity is not None and hasattr(entity, "deleted_at") and entity.deleted_at is not None:
            return None
        return entity

    async def find_all(self, **filters: Any) -> list[T]:
        """Find all, excluding soft-deleted entities."""
        session = self._require_session()
        stmt = select(self._model).where(self._model.deleted_at == None)  # type: ignore[attr-defined]  # noqa: E711
        for key, value in filters.items():
            stmt = stmt.where(getattr(self._model, key) == value)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def find_all_including_deleted(self, **filters: Any) -> list[T]:
        """Find all entities INCLUDING soft-deleted ones."""
        return await super().find_all(**filters)

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

    async def hard_delete(self, id: ID) -> None:
        """Permanently delete an entity (bypass soft delete)."""
        await super().delete(id)

    async def delete_all(self, ids: list[ID]) -> int:
        """Soft-delete all entities with given IDs."""
        if not ids:
            return 0
        session = self._require_session()
        stmt = (
            sa_update(self._model)
            .where(self._model.id.in_(ids))  # type: ignore[attr-defined]
            .values(deleted_at=datetime.now(UTC))
        )
        result = await session.execute(stmt)
        await session.flush()
        return cast(int, result.rowcount)  # type: ignore[attr-defined]

    async def count(self) -> int:
        """Count non-deleted entities."""
        session = self._require_session()
        stmt = (
            select(func.count())
            .select_from(self._model)
            .where(
                self._model.deleted_at == None  # type: ignore[attr-defined]  # noqa: E711
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one()

    # The base paginated / by-ids / by-spec readers do not know about soft
    # deletes, so they would leak deleted rows and miscount the page total.
    # Override every read path to apply the not-deleted predicate (audit #103).

    @property
    def _active(self) -> Any:
        return self._model.deleted_at == None  # type: ignore[attr-defined]  # noqa: E711

    async def find_paginated(self, page: int = 1, size: int = 20, pageable: Any | None = None) -> Any:
        session = self._require_session()
        if pageable is not None:
            page = pageable.page
            size = pageable.size

        from pyfly.data.page import Page

        total = (await session.execute(select(func.count()).select_from(self._model).where(self._active))).scalar_one()

        stmt = select(self._model).where(self._active)
        if pageable is not None:
            stmt = self._apply_sort(stmt, pageable)
        stmt = stmt.offset((page - 1) * size).limit(size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=page, size=size)

    async def find_all_by_ids(self, ids: list[ID]) -> list[T]:
        if not ids:
            return []
        session = self._require_session()
        stmt = select(self._model).where(self._pk_column.in_(ids)).where(self._active)
        return list((await session.execute(stmt)).scalars().all())

    async def find_all_by_spec(self, spec: Any) -> list[T]:
        session = self._require_session()
        stmt = spec.to_predicate(self._model, select(self._model)).where(self._active)
        return list((await session.execute(stmt)).scalars().all())

    async def find_all_by_spec_paged(self, spec: Any, pageable: Any) -> Any:
        session = self._require_session()
        from pyfly.data.page import Page

        filtered = spec.to_predicate(self._model, select(self._model)).where(self._active)
        total = (await session.execute(select(func.count()).select_from(filtered.subquery()))).scalar_one()
        stmt = self._apply_sort(filtered, pageable).offset(pageable.offset).limit(pageable.size)
        items = list((await session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)
