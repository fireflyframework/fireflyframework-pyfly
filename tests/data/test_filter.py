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
"""Tests for FilterUtils and FilterOperator — dynamic query building."""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.filter import FilterOperator, FilterUtils
from pyfly.data.relational.sqlalchemy.specification import Specification

# ---------------------------------------------------------------------------
# Test entity
# ---------------------------------------------------------------------------


class User(BaseEntity):
    __tablename__ = "filter_users"

    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50), default="user")
    age: Mapped[int] = mapped_column(default=25)
    active: Mapped[bool] = mapped_column(default=True)
    bio: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    """Seed the database with a known set of users."""
    users = [
        User(name="Alice", role="admin", age=30, active=True, bio=None),
        User(name="Bob", role="user", age=25, active=True, bio=None),
        User(name="Charlie", role="admin", age=40, active=False, bio=None),
        User(name="Diana", role="user", age=22, active=False, bio="Hello"),
    ]
    session.add_all(users)
    await session.flush()
    return session


# ---------------------------------------------------------------------------
# Helper — execute a spec and return matching user names (sorted)
# ---------------------------------------------------------------------------


async def _names(session: AsyncSession, spec: Specification[User]) -> list[str]:
    """Apply *spec* and return sorted list of matching user names."""
    stmt = select(User)
    stmt = spec.to_predicate(User, stmt)
    result = await session.execute(stmt)
    return sorted(u.name for u in result.scalars().all())


# ---------------------------------------------------------------------------
# FilterOperator tests
# ---------------------------------------------------------------------------


class TestFilterOperatorEq:
    """FilterOperator.eq — equal to."""

    @pytest.mark.asyncio
    async def test_eq_by_name(self, seeded_session: AsyncSession):
        spec = FilterOperator.eq("name", "Alice")
        names = await _names(seeded_session, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_eq_by_role(self, seeded_session: AsyncSession):
        spec = FilterOperator.eq("role", "admin")
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]


class TestFilterOperatorNeq:
    """FilterOperator.neq — not equal to."""

    @pytest.mark.asyncio
    async def test_neq_by_role(self, seeded_session: AsyncSession):
        spec = FilterOperator.neq("role", "admin")
        names = await _names(seeded_session, spec)
        assert names == ["Bob", "Diana"]


class TestFilterOperatorComparisons:
    """FilterOperator gt, gte, lt, lte — numeric comparisons."""

    @pytest.mark.asyncio
    async def test_gt_age(self, seeded_session: AsyncSession):
        spec = FilterOperator.gt("age", 25)
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_gte_age(self, seeded_session: AsyncSession):
        spec = FilterOperator.gte("age", 25)
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie"]

    @pytest.mark.asyncio
    async def test_lt_age(self, seeded_session: AsyncSession):
        spec = FilterOperator.lt("age", 25)
        names = await _names(seeded_session, spec)
        assert names == ["Diana"]

    @pytest.mark.asyncio
    async def test_lte_age(self, seeded_session: AsyncSession):
        spec = FilterOperator.lte("age", 25)
        names = await _names(seeded_session, spec)
        assert names == ["Bob", "Diana"]


class TestFilterOperatorLike:
    """FilterOperator.like — SQL LIKE pattern match."""

    @pytest.mark.asyncio
    async def test_like_pattern(self, seeded_session: AsyncSession):
        spec = FilterOperator.like("name", "A%")
        names = await _names(seeded_session, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_like_pattern_multiple(self, seeded_session: AsyncSession):
        spec = FilterOperator.like("name", "%li%")
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]


class TestFilterOperatorContains:
    """FilterOperator.contains — string contains."""

    @pytest.mark.asyncio
    async def test_contains(self, seeded_session: AsyncSession):
        spec = FilterOperator.contains("name", "li")
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]


class TestFilterOperatorInList:
    """FilterOperator.in_list — value is in list."""

    @pytest.mark.asyncio
    async def test_in_list(self, seeded_session: AsyncSession):
        spec = FilterOperator.in_list("role", ["admin", "moderator"])
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_in_list_names(self, seeded_session: AsyncSession):
        spec = FilterOperator.in_list("name", ["Bob", "Diana"])
        names = await _names(seeded_session, spec)
        assert names == ["Bob", "Diana"]


class TestFilterOperatorNull:
    """FilterOperator.is_null / is_not_null — NULL checks."""

    @pytest.mark.asyncio
    async def test_is_null(self, seeded_session: AsyncSession):
        spec = FilterOperator.is_null("bio")
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie"]

    @pytest.mark.asyncio
    async def test_is_not_null(self, seeded_session: AsyncSession):
        spec = FilterOperator.is_not_null("bio")
        names = await _names(seeded_session, spec)
        assert names == ["Diana"]


class TestFilterOperatorBetween:
    """FilterOperator.between — value is between low and high (inclusive)."""

    @pytest.mark.asyncio
    async def test_between_age(self, seeded_session: AsyncSession):
        spec = FilterOperator.between("age", 25, 35)
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob"]


# ---------------------------------------------------------------------------
# FilterUtils tests
# ---------------------------------------------------------------------------


class TestFilterUtilsBy:
    """FilterUtils.by(**kwargs) — keyword-argument based filtering."""

    @pytest.mark.asyncio
    async def test_by_single_field(self, seeded_session: AsyncSession):
        spec = FilterUtils.by(name="Alice")
        names = await _names(seeded_session, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_by_multiple_fields(self, seeded_session: AsyncSession):
        spec = FilterUtils.by(name="Alice", active=True)
        names = await _names(seeded_session, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_by_no_match(self, seeded_session: AsyncSession):
        spec = FilterUtils.by(name="Alice", active=False)
        names = await _names(seeded_session, spec)
        assert names == []


class TestFilterUtilsFromDict:
    """FilterUtils.from_dict — dict-based filtering."""

    @pytest.mark.asyncio
    async def test_from_dict(self, seeded_session: AsyncSession):
        spec = FilterUtils.from_dict({"role": "admin", "active": True})
        names = await _names(seeded_session, spec)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_from_dict_skips_none(self, seeded_session: AsyncSession):
        spec = FilterUtils.from_dict({"role": "admin", "name": None})
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]


class TestFilterUtilsFromExample:
    """FilterUtils.from_example — entity/dataclass-based filtering."""

    @pytest.mark.asyncio
    async def test_from_example_dataclass(self, seeded_session: AsyncSession):
        @dataclasses.dataclass
        class UserFilter:
            name: str | None = None
            role: str | None = None
            age: int | None = None
            active: bool | None = None
            bio: str | None = None

        example = UserFilter(role="admin")
        spec = FilterUtils.from_example(example)
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_from_example_plain_object(self, seeded_session: AsyncSession):
        class UserFilter:
            def __init__(self):
                self.name = "Bob"
                self.role = None
                self.age = None
                self.active = None
                self.bio = None

        example = UserFilter()
        spec = FilterUtils.from_example(example)
        names = await _names(seeded_session, spec)
        assert names == ["Bob"]


class TestFilterUtilsEmpty:
    """Empty filters should return all records."""

    @pytest.mark.asyncio
    async def test_by_empty(self, seeded_session: AsyncSession):
        spec = FilterUtils.by()
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_dict_empty(self, seeded_session: AsyncSession):
        spec = FilterUtils.from_dict({})
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_dict_all_none(self, seeded_session: AsyncSession):
        spec = FilterUtils.from_dict({"name": None, "role": None})
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_from_example_all_none(self, seeded_session: AsyncSession):
        @dataclasses.dataclass
        class UserFilter:
            name: str | None = None
            role: str | None = None

        spec = FilterUtils.from_example(UserFilter())
        names = await _names(seeded_session, spec)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]
