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

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import Document, init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.repository import MongoRepository


class Widget(Document):
    name: str

    class Settings:
        name = "widgets"


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["testdb"], document_models=[Widget])
    yield
    # Cleanup
    await Widget.find_all().delete()


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
    async def test_find_all_by_ids(self, repo):
        widgets = await repo.save_all([Widget(name="x"), Widget(name="y"), Widget(name="z")])
        ids = [w.id for w in widgets]

        found = await repo.find_all_by_ids(ids[:2])
        assert len(found) == 2

    @pytest.mark.asyncio
    async def test_find_all_by_ids_empty(self, repo):
        found = await repo.find_all_by_ids([])
        assert found == []

    @pytest.mark.asyncio
    async def test_delete_all_by_ids(self, repo):
        widgets = await repo.save_all([Widget(name="a"), Widget(name="b"), Widget(name="c")])
        ids = [w.id for w in widgets]

        deleted = await repo.delete_all(ids[:2])
        assert deleted == 2
        assert await repo.count() == 1

    @pytest.mark.asyncio
    async def test_delete_all_by_ids_empty(self, repo):
        deleted = await repo.delete_all([])
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_all_entities(self, repo):
        widgets = await repo.save_all([Widget(name="a"), Widget(name="b")])
        deleted = await repo.delete_all_entities(widgets)
        assert deleted == 2
        assert await repo.count() == 0
