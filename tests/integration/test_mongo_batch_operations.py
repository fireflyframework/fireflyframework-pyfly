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
"""Tests for batch operations on MongoRepository."""

from collections.abc import AsyncIterator

import pytest
from beanie import Document

from pyfly.data.document.mongodb.repository import MongoRepository
from tests.support.mongo import beanie_database


class Widget(Document):
    name: str

    class Settings:
        name = "widgets"


@pytest.fixture
async def db(mongo_rs_url: str) -> AsyncIterator[None]:
    """Bind the documents to a database of the test's own on the MongoDB replica set."""
    async with beanie_database(mongo_rs_url, [Widget]):
        yield


@pytest.fixture
def repo(db):
    return MongoRepository[Widget, str](Widget)


class TestMongoBatchOperations:
    @pytest.mark.asyncio
    async def test_save_all(self, repo):
        widgets = [Widget(name="a"), Widget(name="b"), Widget(name="c")]
        result = await repo.save_all(widgets)
        assert len(result) == 3
        assert all(w.id is not None for w in result)

    @pytest.mark.asyncio
    async def test_find_all_by_id(self, repo):
        widgets = await repo.save_all([Widget(name="x"), Widget(name="y"), Widget(name="z")])
        ids = [w.id for w in widgets]

        found = await repo.find_all_by_id(ids[:2])
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_find_all_by_id_empty(self, repo):
        found = await repo.find_all_by_id([])
        assert found == []

    @pytest.mark.asyncio
    async def test_delete_all_by_id(self, repo):
        widgets = await repo.save_all([Widget(name="a"), Widget(name="b"), Widget(name="c")])
        ids = [w.id for w in widgets]

        result = await repo.delete_all_by_id(ids[:2])
        assert result is None
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_delete_all_by_id_empty(self, repo):
        await repo.save_all([Widget(name="a")])
        await repo.delete_all_by_id([])
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_delete_all_entities(self, repo):
        widgets = await repo.save_all([Widget(name="a"), Widget(name="b")])
        result = await repo.delete_all(widgets)
        assert result is None
        assert await repo.count() == 0

    @pytest.mark.asyncio
    async def test_delete_all_truncate(self, repo):
        await repo.save_all([Widget(name="a"), Widget(name="b"), Widget(name="c")])
        await repo.delete_all()
        assert await repo.count() == 0
