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

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, get_args, get_origin, overload

import pymongo

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort

if TYPE_CHECKING:
    from pyfly.data.document.mongodb.specification import MongoSpecification

T = TypeVar("T")
ID = TypeVar("ID")


class MongoRepository(Generic[T, ID]):
    """Generic CRUD repository for Beanie documents.

    Mirrors ``Repository[T, ID]`` from the SQLAlchemy adapter — implementing the
    Spring-parity ``PagingAndSortingRepository`` contract — but operates against
    MongoDB via Beanie ODM. No session injection — Beanie uses a globally
    initialised pymongo async client.

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

    def _query(self, **filters: Any) -> Any:
        """Build a Beanie find-query, optionally filtered by field equality."""
        if filters:
            return self._model.find(filters)  # type: ignore[attr-defined]
        return self._model.find_all()  # type: ignore[attr-defined]

    @staticmethod
    def _sort_spec(sort: Sort) -> list[tuple[str, int]]:
        """Build a pymongo sort specification from a :class:`Sort`."""
        return [
            (order.property, pymongo.ASCENDING if order.direction == "asc" else pymongo.DESCENDING)
            for order in sort.orders
        ]

    # ------------------------------------------------------------------
    # CrudRepository
    # ------------------------------------------------------------------

    async def save(self, entity: T) -> T:
        """Persist a document (insert or update)."""
        await entity.save()  # type: ignore[attr-defined]
        return entity

    async def save_all(self, entities: list[T]) -> list[T]:
        """Persist multiple documents."""
        if not entities:
            return []
        result = await self._model.insert_many(entities)  # type: ignore[attr-defined]
        for entity, oid in zip(entities, result.inserted_ids, strict=False):
            entity.id = oid  # type: ignore[attr-defined]
        return entities

    async def find_by_id(self, id: ID) -> T | None:
        """Find a document by its primary key."""
        return cast("T | None", await self._model.get(id))  # type: ignore[attr-defined]

    async def find_all_by_id(self, ids: list[ID]) -> list[T]:
        """Find all documents with IDs in the given list (Spring ``findAllById``)."""
        if not ids:
            return []
        return cast(
            list[T],
            await self._model.find({"_id": {"$in": ids}}).to_list(),  # type: ignore[attr-defined]
        )

    async def exists_by_id(self, id: ID) -> bool:
        """Check whether a document with the given id exists (Spring ``existsById``)."""
        return await self.find_by_id(id) is not None

    async def count(self) -> int:
        """Return the total number of documents."""
        return cast(int, await self._model.find_all().count())  # type: ignore[attr-defined]

    async def delete(self, entity: T) -> None:
        """Delete a document instance (Spring ``delete(entity)``)."""
        await entity.delete()  # type: ignore[attr-defined]

    async def delete_by_id(self, id: ID) -> None:
        """Delete a document by its primary key (Spring ``deleteById``)."""
        entity = await self.find_by_id(id)
        if entity is not None:
            await entity.delete()  # type: ignore[attr-defined]

    async def delete_all_by_id(self, ids: list[ID]) -> None:
        """Delete all documents whose ids are in ``ids`` (Spring ``deleteAllById``)."""
        if not ids:
            return
        await self._model.find({"_id": {"$in": ids}}).delete()  # type: ignore[attr-defined]

    async def delete_all(self, entities: list[T] | None = None) -> None:
        """Delete the given documents, or ALL when ``entities`` is ``None`` (Spring ``deleteAll``)."""
        if entities is None:
            await self._model.delete_all()  # type: ignore[attr-defined]
            return
        for entity in entities:
            await entity.delete()  # type: ignore[attr-defined]

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

        - ``find_all()`` / ``find_all(active=True)`` → ``list[T]`` (optionally filtered)
        - ``find_all(Sort.by("name"))`` → sorted ``list[T]``
        - ``find_all(Pageable.of(1, 20))`` → ``Page[T]``
        """
        if isinstance(criteria, Pageable):
            return await self._find_page(criteria, **filters)
        query = self._query(**filters)
        if isinstance(criteria, Sort):
            spec = self._sort_spec(criteria)
            if spec:
                query = query.sort(spec)
        return cast(list[T], await query.to_list())

    async def stream_all(self, criteria: Sort | None = None, **filters: Any) -> AsyncIterator[T]:
        """Stream documents lazily (``Flux<T>`` analogue) via an async cursor."""
        query = self._query(**filters)
        if criteria is not None:
            spec = self._sort_spec(criteria)
            if spec:
                query = query.sort(spec)
        async for doc in query:
            yield cast(T, doc)

    async def _find_page(self, pageable: Pageable, **filters: Any) -> Page[T]:
        total = await self._query(**filters).count()
        query = self._query(**filters)
        spec = self._sort_spec(pageable.sort)
        if spec:
            query = query.sort(spec)
        items = await query.skip(pageable.offset).limit(pageable.size).to_list()
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)

    # ------------------------------------------------------------------
    # Specification extensions (PyFly)
    # ------------------------------------------------------------------

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

        sort_spec = self._sort_spec(pageable.sort)
        if sort_spec:
            query = query.sort(sort_spec)

        items = await query.skip(pageable.offset).limit(pageable.size).to_list()
        return Page(items=items, total=total, page=pageable.page, size=pageable.size)
