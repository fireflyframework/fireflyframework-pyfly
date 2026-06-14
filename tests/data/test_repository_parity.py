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
"""Spring-parity surface tests for the SQLAlchemy Repository.

Covers the additions over the legacy CRUD set: protocol conformance, the
``find_all`` overload family (``Sort`` / ``Pageable``), ``stream_all``
(``Flux<T>`` analogue), and the renamed delete contract.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.ports.outbound import (
    CrudRepository,
    PagingAndSortingRepository,
    ReactiveSortingRepository,
)
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.repository import Repository


class ParityWidget(BaseEntity):
    __tablename__ = "parity_widgets"

    name: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def repo(session):
    return Repository(ParityWidget, session)


class TestConformance:
    def test_satisfies_protocol_hierarchy(self, repo):
        assert isinstance(repo, CrudRepository)
        assert isinstance(repo, ReactiveSortingRepository)
        assert isinstance(repo, PagingAndSortingRepository)


class TestFindAllFamily:
    @pytest.mark.asyncio
    async def test_find_all_unsorted(self, repo):
        for n in ("A", "B"):
            await repo.save(ParityWidget(name=n))
        assert len(await repo.find_all()) == 2

    @pytest.mark.asyncio
    async def test_find_all_sorted(self, repo):
        for n in ("C", "A", "B"):
            await repo.save(ParityWidget(name=n))
        names = [w.name for w in await repo.find_all(Sort.by("name"))]
        assert names == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_find_all_sorted_desc(self, repo):
        for n in ("A", "B", "C"):
            await repo.save(ParityWidget(name=n))
        names = [w.name for w in await repo.find_all(Sort(orders=(Order.desc("name"),)))]
        assert names == ["C", "B", "A"]

    @pytest.mark.asyncio
    async def test_find_all_filtered(self, repo):
        await repo.save(ParityWidget(name="keep"))
        await repo.save(ParityWidget(name="drop"))
        assert len(await repo.find_all(name="keep")) == 1

    @pytest.mark.asyncio
    async def test_find_all_sorted_and_filtered(self, repo):
        await repo.save(ParityWidget(name="keep"))
        await repo.save(ParityWidget(name="keep"))
        await repo.save(ParityWidget(name="drop"))
        rows = await repo.find_all(Sort.by("name"), name="keep")
        assert [w.name for w in rows] == ["keep", "keep"]

    @pytest.mark.asyncio
    async def test_find_all_pageable_returns_page(self, repo):
        for i in range(15):
            await repo.save(ParityWidget(name=f"w{i:02d}"))
        page = await repo.find_all(Pageable.of(1, 5, Sort.by("name")))
        assert isinstance(page, Page)
        assert page.total == 15
        assert [w.name for w in page.items] == ["w00", "w01", "w02", "w03", "w04"]

    @pytest.mark.asyncio
    async def test_find_all_pageable_filtered_total(self, repo):
        for i in range(6):
            await repo.save(ParityWidget(name="x" if i % 2 else "y"))
        page = await repo.find_all(Pageable.of(1, 10), name="x")
        assert page.total == 3
        assert len(page.items) == 3


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_all_yields_all_sorted(self, repo):
        for n in ("C", "A", "B"):
            await repo.save(ParityWidget(name=n))
        seen = [w.name async for w in repo.stream_all(Sort.by("name"))]
        assert seen == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_stream_all_filtered(self, repo):
        await repo.save(ParityWidget(name="keep"))
        await repo.save(ParityWidget(name="drop"))
        seen = [w.name async for w in repo.stream_all(name="keep")]
        assert seen == ["keep"]


class TestDeletes:
    @pytest.mark.asyncio
    async def test_delete_entity(self, repo):
        w = await repo.save(ParityWidget(name="x"))
        result = await repo.delete(w)
        assert result is None
        assert await repo.exists_by_id(w.id) is False

    @pytest.mark.asyncio
    async def test_delete_by_id(self, repo):
        w = await repo.save(ParityWidget(name="x"))
        await repo.delete_by_id(w.id)
        assert await repo.find_by_id(w.id) is None

    @pytest.mark.asyncio
    async def test_delete_by_id_missing_is_noop(self, repo):
        from uuid import uuid4

        await repo.delete_by_id(uuid4())  # must not raise

    @pytest.mark.asyncio
    async def test_delete_all_by_id(self, repo):
        ws = [await repo.save(ParityWidget(name=f"n{i}")) for i in range(3)]
        await repo.delete_all_by_id([ws[0].id, ws[1].id])
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_delete_all_entities(self, repo):
        ws = [await repo.save(ParityWidget(name=f"n{i}")) for i in range(3)]
        await repo.delete_all(ws)
        assert await repo.count() == 0

    @pytest.mark.asyncio
    async def test_delete_all_truncate(self, repo):
        for i in range(4):
            await repo.save(ParityWidget(name=f"n{i}"))
        await repo.delete_all()
        assert await repo.count() == 0
