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
"""Beanie documents are never cached (C022, C025), against a MongoDB replica set.

A document loaded from MongoDB carries its revision and state, and belongs to the session that loaded
it, as an ORM entity does. Every backend refuses it: the in-memory cache (it used to hand every caller
the same live document), the JSON encoder of the Redis and PostgreSQL caches (it used to dump it as a
``dict``), the decorators when the declared return type is a document, and the CQRS query bus when a
handler's result type is one. A DTO built from the document caches as usual.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from beanie import Document, init_beanie
from pydantic import BaseModel
from pymongo import AsyncMongoClient

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.decorators import cacheable
from pyfly.cache.serialization import CacheValueError, cache_dumps
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from tests.support.backend_matrix import MongoBackend


class CachedNote(Document):
    title: str
    body: str = ""

    class Settings:
        name = "wp14_cached_notes"


class NoteDto(BaseModel):
    title: str
    body: str


@dataclass(frozen=True)
class GetNoteQuery(Query[CachedNote | None]):
    title: str = ""


@pytest.fixture
async def note(mongo_backend: MongoBackend) -> AsyncIterator[CachedNote]:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
    try:
        await init_beanie(database=client[mongo_backend.database], document_models=[CachedNote])
        await CachedNote(title="minutes", body="first draft").insert()
        found = await CachedNote.find_one(CachedNote.title == "minutes")
        assert found is not None
        yield found
    finally:
        await client.close()


async def test_every_backend_refuses_a_loaded_document(note: CachedNote) -> None:
    cache = InMemoryCache()
    with pytest.raises(CacheValueError, match="CachedNote"):
        await cache.put("note", note)
    with pytest.raises(CacheValueError, match="CachedNote"):
        await cache.put("notes", {"page": [note]})
    with pytest.raises(CacheValueError, match="CachedNote"):
        cache_dumps(note)  # the Redis and PostgreSQL encoding
    assert cache.get_keys() == []


async def test_a_document_return_type_is_refused_and_a_dto_is_cached(
    note: CachedNote, caplog: pytest.LogCaptureFixture
) -> None:
    cache = InMemoryCache()
    with pytest.raises(TypeError, match="CachedNote"):

        @cacheable(cache, key="note:{title}")
        async def get_note(title: str) -> CachedNote | None: ...

    @cacheable(cache, key="loose:{title}")
    async def get_loose(title: str):  # noqa: ANN202 — unannotated on purpose: refused at run time
        return await CachedNote.find_one(CachedNote.title == title)

    @cacheable(cache, key="dto:{title}")
    async def get_dto(title: str) -> NoteDto | None:
        found = await CachedNote.find_one(CachedNote.title == title)
        return NoteDto(title=found.title, body=found.body) if found else None

    caplog.set_level(logging.WARNING, logger="pyfly.cache")
    loaded = await get_loose("minutes")
    assert isinstance(loaded, CachedNote)
    assert await cache.exists("loose:minutes") is False
    assert any("CachedNote" in record.getMessage() for record in caplog.records)

    dto = await get_dto("minutes")
    dto.body = "changed by one caller"
    assert await get_dto("minutes") == NoteDto(title="minutes", body="first draft")


async def test_the_query_bus_never_caches_a_document_result_type(
    note: CachedNote, caplog: pytest.LogCaptureFixture
) -> None:
    @query_handler(cacheable=True)
    class GetNoteHandler(QueryHandler[GetNoteQuery, CachedNote | None]):
        calls = 0

        async def do_handle(self, query: GetNoteQuery) -> CachedNote | None:
            type(self).calls += 1
            return await CachedNote.find_one(CachedNote.title == query.title)

    cache = InMemoryCache()
    registry = HandlerRegistry()
    registry.register_query_handler(GetNoteHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    caller = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        result = await bus.query_with_context(GetNoteQuery(title="minutes"), caller)
        assert isinstance(result, CachedNote)
        assert result.body == "first draft"
    assert GetNoteHandler.calls == 2
    assert cache.get_keys() == []
    assert [r.getMessage() for r in caplog.records if "query_cache_disabled" in r.getMessage()] != []
