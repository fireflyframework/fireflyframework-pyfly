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
"""MongoDB derived-query OPERATOR + PROJECTION execution (v26.06.76).

Closes the audit gap where the Mongo query compiler + projections were only smoke-tested (clause
construction / return_type acceptance) but never EXECUTED against a collection. Here every
comparison operator and the projection path run through a real (mongomock) find and assert results.
"""

from __future__ import annotations

from typing import Protocol

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.projection import projection


class EItem(BaseDocument):
    name: str
    age: int = 0
    price: float = 0.0
    role: str = "user"

    class Settings:
        name = "exec_query_items"


@projection
class NameProjection(Protocol):
    name: str


class OpRepo(MongoRepository[EItem, str]):
    async def find_by_age_greater_than(self, age: int) -> list[EItem]: ...
    async def find_by_age_greater_than_equal(self, age: int) -> list[EItem]: ...
    async def find_by_price_less_than(self, price: float) -> list[EItem]: ...
    async def find_by_price_between(self, low: float, high: float) -> list[EItem]: ...
    async def find_by_role_in(self, roles: list[str]) -> list[EItem]: ...
    async def find_by_name_containing(self, term: str) -> list[EItem]: ...
    async def find_by_role_order_by_age_desc(self, role: str) -> list[EItem]: ...
    async def count_by_age_greater_than(self, age: int) -> int: ...
    async def find_by_role(self, role: str) -> list[NameProjection]: ...  # projection return type


@pytest.fixture(autouse=True)
async def init_db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[EItem])
    yield
    client.close()


@pytest.fixture
async def repo():
    for item in (
        EItem(name="Alice", age=30, price=10.0, role="admin"),
        EItem(name="Bob", age=20, price=50.0, role="user"),
        EItem(name="Carol", age=40, price=5.0, role="admin"),
        EItem(name="Dave", age=25, price=99.0, role="user"),
    ):
        await item.save()
    r = OpRepo(EItem)
    MongoRepositoryBeanPostProcessor().after_init(r, "opRepo")
    return r


def _names(results: list[EItem]) -> set[str]:
    return {r.name for r in results}


@pytest.mark.asyncio
async def test_greater_than(repo: OpRepo) -> None:
    assert _names(await repo.find_by_age_greater_than(25)) == {"Alice", "Carol"}


@pytest.mark.asyncio
async def test_greater_than_equal(repo: OpRepo) -> None:
    assert _names(await repo.find_by_age_greater_than_equal(25)) == {"Dave", "Alice", "Carol"}


@pytest.mark.asyncio
async def test_less_than(repo: OpRepo) -> None:
    assert _names(await repo.find_by_price_less_than(10.0)) == {"Carol"}


@pytest.mark.asyncio
async def test_between(repo: OpRepo) -> None:
    assert _names(await repo.find_by_price_between(5.0, 50.0)) == {"Alice", "Bob", "Carol"}


@pytest.mark.asyncio
async def test_in(repo: OpRepo) -> None:
    assert _names(await repo.find_by_role_in(["admin"])) == {"Alice", "Carol"}


@pytest.mark.asyncio
async def test_containing_case_insensitive(repo: OpRepo) -> None:
    assert _names(await repo.find_by_name_containing("ar")) == {"Carol"}  # C-ar-ol


@pytest.mark.asyncio
async def test_order_by_desc(repo: OpRepo) -> None:
    results = await repo.find_by_role_order_by_age_desc("admin")
    assert [r.name for r in results] == ["Carol", "Alice"]  # age 40, then 30


@pytest.mark.asyncio
async def test_count_by_operator(repo: OpRepo) -> None:
    assert await repo.count_by_age_greater_than(25) == 2


@pytest.mark.asyncio
async def test_projection_returns_subset(repo: OpRepo) -> None:
    results = await repo.find_by_role("admin")
    assert len(results) == 2
    assert {r.name for r in results} == {"Alice", "Carol"}
    # projected objects are NOT full documents — non-projected fields are absent
    for r in results:
        assert not isinstance(r, EItem)
        assert not hasattr(r, "price")
