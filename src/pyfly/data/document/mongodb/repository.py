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
"""Generic async repository built on Beanie ODM for MongoDB."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, get_args, get_origin

import pymongo

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable

if TYPE_CHECKING:
    from pyfly.data.document.mongodb.specification import MongoSpecification

T = TypeVar("T")
ID = TypeVar("ID")


class MongoRepository(Generic[T, ID]):
    """Generic CRUD repository for Beanie documents.

    Mirrors ``Repository[T, ID]`` from the SQLAlchemy adapter but operates
    against MongoDB via Beanie ODM. No session injection — Beanie uses a
    globally initialised Motor client.

    Type Parameters:
        T: The document type (Beanie Document subclass).
        ID: The primary key type (typically ``PydanticObjectId`` or ``str``).

    Usage::

        class UserDocumentRepository(MongoRepository[UserDocument, str]):
            pass  # entity type auto-extracted, no explicit model needed
    """

    _entity_type: type | None = None
    _id_type: type | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in getattr(cls, "__orig_bases__", []):
            origin = get_origin(base)
            if origin is MongoRepository:
                args = get_args(base)
                if args and not isinstance(args[0], TypeVar):
                    cls._entity_type = args[0]
                if len(args) > 1 and not isinstance(args[1], TypeVar):
                    cls._id_type = args[1]
                break

    def __init__(self, model: type[T] | None = None) -> None:
        resolved = model or getattr(type(self), "_entity_type", None)
        if resolved is None:
            raise TypeError(
                f"{type(self).__name__} requires either MongoRepository[Document, ID] "
                f"declaration or explicit model argument"
            )
        self._model: type[T] = cast(type[T], resolved)

    async def save(self, entity: T) -> T:
        """Persist a document (insert or update)."""
        await entity.save()  # type: ignore[attr-defined]
        return entity

    async def find_by_id(self, id: ID) -> T | None:
        """Find a document by its primary key."""
        return cast("T | None", await self._model.get(id))  # type: ignore[attr-defined]

    async def find_all(self, **filters: Any) -> list[T]:
        """Find all documents, optionally filtered by field values."""
        if filters:
            return cast(list[T], await self._model.find(filters).to_list())  # type: ignore[attr-defined]
        return cast(list[T], await self._model.find_all().to_list())  # type: ignore[attr-defined]

    async def delete(self, id: ID) -> None:
        """Delete a document by its primary key."""
        entity = await self.find_by_id(id)
        if entity is not None:
            await entity.delete()  # type: ignore[attr-defined]

    async def find_paginated(self, page: int = 1, size: int = 20, pageable: Pageable | None = None) -> Page[T]:
        """Find documents with pagination.

        Args:
            page: Page number (1-based).
            size: Number of items per page.
            pageable: Optional Pageable with page, size, and sort criteria.
        """
        if pageable is not None:
            page = pageable.page
            size = pageable.size

        total = await self._model.find_all().count()  # type: ignore[attr-defined]
        offset = (page - 1) * size

        query = self._model.find_all()  # type: ignore[attr-defined]

        if pageable is not None:
            sort_spec = self._build_sort(pageable)
            if sort_spec:
                query = query.sort(sort_spec)

        items = await query.skip(offset).limit(size).to_list()
        return Page(items=items, total=total, page=page, size=size)

    async def count(self) -> int:
        """Return the total number of documents."""
        return cast(int, await self._model.find_all().count())  # type: ignore[attr-defined]

    async def exists(self, id: ID) -> bool:
        """Check if a document with the given ID exists."""
        entity = await self.find_by_id(id)
        return entity is not None

    async def find_all_by_spec(self, spec: MongoSpecification[T]) -> list[T]:
        """Find all documents matching a specification."""
        filter_doc = spec.to_predicate(self._model, {})
        if filter_doc:
            return cast(list[T], await self._model.find(filter_doc).to_list())  # type: ignore[attr-defined]
        return cast(list[T], await self._model.find_all().to_list())  # type: ignore[attr-defined]

    async def find_all_by_spec_paged(self, spec: MongoSpecification[T], pageable: Pageable) -> Page[T]:
        """Find documents matching a specification with pagination."""
        filter_doc = spec.to_predicate(self._model, {})
        if filter_doc:
            total = await self._model.find(filter_doc).count()  # type: ignore[attr-defined]
            query = self._model.find(filter_doc)  # type: ignore[attr-defined]
        else:
            total = await self._model.find_all().count()  # type: ignore[attr-defined]
            query = self._model.find_all()  # type: ignore[attr-defined]

        sort_spec = self._build_sort(pageable)
        if sort_spec:
            query = query.sort(sort_spec)

        items = await query.skip(pageable.offset).limit(pageable.size).to_list()
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    @staticmethod
    def _build_sort(pageable: Pageable) -> list[tuple[str, int]]:
        """Build a pymongo sort specification from a Pageable."""
        sort_spec: list[tuple[str, int]] = []
        for order in pageable.sort.orders:
            direction = pymongo.ASCENDING if order.direction == "asc" else pymongo.DESCENDING
            sort_spec.append((order.property, direction))
        return sort_spec

    async def save_all(self, entities: list[T]) -> list[T]:
        """Persist multiple documents."""
        if not entities:
            return []
        result = await self._model.insert_many(entities)  # type: ignore[attr-defined]
        for entity, oid in zip(entities, result.inserted_ids, strict=False):
            entity.id = oid  # type: ignore[attr-defined]
        return entities

    async def find_all_by_ids(self, ids: list[ID]) -> list[T]:
        """Find all documents with IDs in the given list."""
        if not ids:
            return []
        return cast(
            list[T],
            await self._model.find(  # type: ignore[attr-defined]
                {"_id": {"$in": ids}}
            ).to_list(),
        )

    async def delete_all(self, ids: list[ID]) -> int:
        """Delete all documents with IDs in the given list. Returns count deleted."""
        if not ids:
            return 0
        result = await self._model.find(  # type: ignore[attr-defined]
            {"_id": {"$in": ids}}
        ).delete()
        return result.deleted_count if result else 0

    async def delete_all_entities(self, entities: list[T]) -> int:
        """Delete all given document instances. Returns count deleted."""
        count = 0
        for entity in entities:
            await entity.delete()  # type: ignore[attr-defined]
            count += 1
        return count
