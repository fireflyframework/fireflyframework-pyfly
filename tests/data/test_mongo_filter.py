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
"""Tests for MongoFilterUtils and MongoFilterOperator — dynamic MongoDB query building."""

from __future__ import annotations

import dataclasses

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor", reason="mongomock-motor not installed")

from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.filter import MongoFilterOperator, MongoFilterUtils
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.specification import MongoSpecification

# ---------------------------------------------------------------------------
# Test document
# ---------------------------------------------------------------------------


class User(BaseDocument):
    name: str
    role: str = "user"
    age: int = 25
    active: bool = True
    bio: str | None = None

    class Settings:
        name = "filter_test_users"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def init_db():
    client = AsyncMongoMockClient()
    await init_beanie(database=client["test_db"], document_models=[User])
    yield
    client.close()


@pytest.fixture
async def seeded_repo() -> MongoRepository[User, str]:
    """Seed the database and return a repository."""
    users = [
        User(name="Alice", role="admin", age=30, active=True, bio=None),
        User(name="Bob", role="user", age=25, active=True, bio=None),
        User(name="Charlie", role="admin", age=40, active=False, bio=None),
        User(name="Diana", role="user", age=22, active=False, bio="Hello"),
    ]
    for u in users:
        await u.save()
    return MongoRepository[User, str](User)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _names(repo: MongoRepository[User, str], spec: MongoSpecification[User]) -> list[str]:
    """Apply *spec* and return sorted list of matching user names."""
    results = await repo.find_all_by_spec(spec)
    return sorted(u.name for u in results)


# ---------------------------------------------------------------------------
# MongoFilterOperator tests
# ---------------------------------------------------------------------------


class TestMongoFilterOperatorEq:
    """MongoFilterOperator.eq — equal to."""

    @pytest.mark.asyncio
    async def test_eq_by_name(self, seeded_repo):
        spec = MongoFilterOperator.eq("name", "Alice")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_eq_by_role(self, seeded_repo):
        spec = MongoFilterOperator.eq("role", "admin")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]


class TestMongoFilterOperatorNeq:
    """MongoFilterOperator.neq — not equal to."""

    @pytest.mark.asyncio
    async def test_neq_by_role(self, seeded_repo):
        spec = MongoFilterOperator.neq("role", "admin")
        names = await _names(seeded_repo, spec)
        assert names == ["Bob", "Diana"]


class TestMongoFilterOperatorComparisons:
    """MongoFilterOperator gt, gte, lt, lte — numeric comparisons."""

    @pytest.mark.asyncio
    async def test_gt_age(self, seeded_repo):
        spec = MongoFilterOperator.gt("age", 25)
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_gte_age(self, seeded_repo):
        spec = MongoFilterOperator.gte("age", 25)
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie"]

    @pytest.mark.asyncio
    async def test_lt_age(self, seeded_repo):
        spec = MongoFilterOperator.lt("age", 25)
        names = await _names(seeded_repo, spec)
        assert names == ["Diana"]

    @pytest.mark.asyncio
    async def test_lte_age(self, seeded_repo):
        spec = MongoFilterOperator.lte("age", 25)
        names = await _names(seeded_repo, spec)
        assert names == ["Bob", "Diana"]


class TestMongoFilterOperatorLike:
    """MongoFilterOperator.like — regex pattern match (SQL LIKE equivalent)."""

    @pytest.mark.asyncio
    async def test_like_pattern(self, seeded_repo):
        spec = MongoFilterOperator.like("name", "A%")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_like_pattern_multiple(self, seeded_repo):
        spec = MongoFilterOperator.like("name", "%li%")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]


class TestMongoFilterOperatorContains:
    """MongoFilterOperator.contains — string contains."""

    @pytest.mark.asyncio
    async def test_contains(self, seeded_repo):
        spec = MongoFilterOperator.contains("name", "li")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]


class TestMongoFilterOperatorInList:
    """MongoFilterOperator.in_list — value is in list."""

    @pytest.mark.asyncio
    async def test_in_list(self, seeded_repo):
        spec = MongoFilterOperator.in_list("role", ["admin", "moderator"])
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_in_list_names(self, seeded_repo):
        spec = MongoFilterOperator.in_list("name", ["Bob", "Diana"])
        names = await _names(seeded_repo, spec)
        assert names == ["Bob", "Diana"]


class TestMongoFilterOperatorNull:
    """MongoFilterOperator.is_null / is_not_null — NULL checks."""

    @pytest.mark.asyncio
    async def test_is_null(self, seeded_repo):
        spec = MongoFilterOperator.is_null("bio")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie"]

    @pytest.mark.asyncio
    async def test_is_not_null(self, seeded_repo):
        spec = MongoFilterOperator.is_not_null("bio")
        names = await _names(seeded_repo, spec)
        assert names == ["Diana"]


class TestMongoFilterOperatorBetween:
    """MongoFilterOperator.between — value is between low and high (inclusive)."""

    @pytest.mark.asyncio
    async def test_between_age(self, seeded_repo):
        spec = MongoFilterOperator.between("age", 25, 35)
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob"]


# ---------------------------------------------------------------------------
# MongoFilterUtils tests
# ---------------------------------------------------------------------------


class TestMongoFilterUtilsBy:
    """MongoFilterUtils.by(**kwargs) — keyword-argument based filtering."""

    @pytest.mark.asyncio
    async def test_by_single_field(self, seeded_repo):
        spec = MongoFilterUtils.by(name="Alice")
        names = await _names(seeded_repo, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_by_multiple_fields(self, seeded_repo):
        spec = MongoFilterUtils.by(name="Alice", active=True)
        names = await _names(seeded_repo, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_by_no_match(self, seeded_repo):
        spec = MongoFilterUtils.by(name="Alice", active=False)
        names = await _names(seeded_repo, spec)
        assert names == []


class TestMongoFilterUtilsFromDict:
    """MongoFilterUtils.from_dict — dict-based filtering."""

    @pytest.mark.asyncio
    async def test_from_dict(self, seeded_repo):
        spec = MongoFilterUtils.from_dict({"role": "admin", "active": True})
        names = await _names(seeded_repo, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_from_dict_skips_none(self, seeded_repo):
        spec = MongoFilterUtils.from_dict({"role": "admin", "name": None})
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]


class TestMongoFilterUtilsFromExample:
    """MongoFilterUtils.from_example — entity/dataclass-based filtering."""

    @pytest.mark.asyncio
    async def test_from_example_dataclass(self, seeded_repo):
        @dataclasses.dataclass
        class UserFilter:
            name: str | None = None
            role: str | None = None
            age: int | None = None
            active: bool | None = None
            bio: str | None = None

        example = UserFilter(role="admin")
        spec = MongoFilterUtils.from_example(example)
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_from_example_plain_object(self, seeded_repo):
        class UserFilter:
            def __init__(self):
                self.name = "Bob"
                self.role = None
                self.age = None
                self.active = None
                self.bio = None

        example = UserFilter()
        spec = MongoFilterUtils.from_example(example)
        names = await _names(seeded_repo, spec)
        assert names == ["Bob"]


class TestMongoFilterUtilsEmpty:
    """Empty filters should return all records."""

    @pytest.mark.asyncio
    async def test_by_empty(self, seeded_repo):
        spec = MongoFilterUtils.by()
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_dict_empty(self, seeded_repo):
        spec = MongoFilterUtils.from_dict({})
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_dict_all_none(self, seeded_repo):
        spec = MongoFilterUtils.from_dict({"name": None, "role": None})
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_example_all_none(self, seeded_repo):
        @dataclasses.dataclass
        class UserFilter:
            name: str | None = None
            role: str | None = None

        spec = MongoFilterUtils.from_example(UserFilter())
        names = await _names(seeded_repo, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]
