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
"""Spring-parity surface tests for MongoRepository (mongomock-backed)."""

from __future__ import annotations

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import Document, init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.ports.outbound import (
    CrudRepository,
    PagingAndSortingRepository,
    ReactiveSortingRepository,
)


class ParityDoc(Document):
    name: str
    active: bool = True

    class Settings:
        name = "parity_docs"


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["paritydb"], document_models=[ParityDoc])
    yield
    await ParityDoc.find_all().delete()


@pytest.fixture
def repo(db):
    return MongoRepository[ParityDoc, str](ParityDoc)


class TestConformance:
    def test_satisfies_protocol_hierarchy(self, repo):
        assert isinstance(repo, CrudRepository)
        assert isinstance(repo, ReactiveSortingRepository)
        assert isinstance(repo, PagingAndSortingRepository)


class TestFindAllFamily:
    @pytest.mark.asyncio
    async def test_find_all_sorted(self, repo):
        for n in ("C", "A", "B"):
            await repo.save(ParityDoc(name=n))
        names = [d.name for d in await repo.find_all(Sort.by("name"))]
        assert names == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_find_all_sorted_desc(self, repo):
        for n in ("A", "B", "C"):
            await repo.save(ParityDoc(name=n))
        names = [d.name for d in await repo.find_all(Sort(orders=(Order.desc("name"),)))]
        assert names == ["C", "B", "A"]

    @pytest.mark.asyncio
    async def test_find_all_filtered(self, repo):
        await repo.save(ParityDoc(name="on", active=True))
        await repo.save(ParityDoc(name="off", active=False))
        rows = await repo.find_all(active=True)
        assert [d.name for d in rows] == ["on"]

    @pytest.mark.asyncio
    async def test_find_all_pageable_returns_page(self, repo):
        for i in range(15):
            await repo.save(ParityDoc(name=f"d{i:02d}"))
        page = await repo.find_all(Pageable.of(1, 5, Sort.by("name")))
        assert isinstance(page, Page)
        assert page.total == 15
        assert [d.name for d in page.items] == ["d00", "d01", "d02", "d03", "d04"]


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_all_yields_all_sorted(self, repo):
        for n in ("C", "A", "B"):
            await repo.save(ParityDoc(name=n))
        seen = [d.name async for d in repo.stream_all(Sort.by("name"))]
        assert seen == ["A", "B", "C"]


class TestDeletes:
    @pytest.mark.asyncio
    async def test_delete_entity(self, repo):
        d = await repo.save(ParityDoc(name="x"))
        result = await repo.delete(d)
        assert result is None
        assert await repo.exists_by_id(d.id) is False

    @pytest.mark.asyncio
    async def test_delete_by_id(self, repo):
        d = await repo.save(ParityDoc(name="x"))
        await repo.delete_by_id(d.id)
        assert await repo.find_by_id(d.id) is None

    @pytest.mark.asyncio
    async def test_delete_all_by_id(self, repo):
        ds = await repo.save_all([ParityDoc(name=f"n{i}") for i in range(3)])
        await repo.delete_all_by_id([ds[0].id, ds[1].id])
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_delete_all_entities(self, repo):
        ds = await repo.save_all([ParityDoc(name=f"n{i}") for i in range(3)])
        await repo.delete_all(ds)
        assert await repo.count() == 0

    @pytest.mark.asyncio
    async def test_delete_all_truncate(self, repo):
        await repo.save_all([ParityDoc(name=f"n{i}") for i in range(4)])
        await repo.delete_all()
        assert await repo.count() == 0
