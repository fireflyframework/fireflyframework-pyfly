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
"""Tests for Specification pattern — composable query predicates."""

from __future__ import annotations

import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.specification import Specification

# ---------------------------------------------------------------------------
# Test entity
# ---------------------------------------------------------------------------


class User(BaseEntity):
    __tablename__ = "spec_users"

    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50), default="user")
    active: Mapped[bool] = mapped_column(default=True)


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
        User(name="Alice", role="admin", active=True),
        User(name="Bob", role="user", active=True),
        User(name="Charlie", role="admin", active=False),
        User(name="Diana", role="user", active=False),
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
# Tests
# ---------------------------------------------------------------------------


class TestSpecificationSingle:
    """A single specification applies its predicate correctly."""

    @pytest.mark.asyncio
    async def test_filter_by_role(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        names = await _names(seeded_session, admin_spec)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_filter_by_active(self, seeded_session: AsyncSession):
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        names = await _names(seeded_session, active_spec)
        assert names == ["Alice", "Bob"]


class TestSpecificationAnd:
    """AND (&) combination — both conditions must be satisfied."""

    @pytest.mark.asyncio
    async def test_and_combination(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        combined = admin_spec & active_spec
        names = await _names(seeded_session, combined)
        assert names == ["Alice"]

    @pytest.mark.asyncio
    async def test_and_three_specs(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        name_spec: Specification[User] = Specification(lambda root, q: q.where(root.name == "Alice"))
        combined = admin_spec & active_spec & name_spec
        names = await _names(seeded_session, combined)
        assert names == ["Alice"]


class TestSpecificationOr:
    """OR (|) combination — either condition may match."""

    @pytest.mark.asyncio
    async def test_or_combination(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        combined = admin_spec | active_spec
        names = await _names(seeded_session, combined)
        # admin: Alice, Charlie  |  active: Alice, Bob  -> union: Alice, Bob, Charlie
        assert names == ["Alice", "Bob", "Charlie"]


class TestSpecificationNot:
    """NOT (~) — negated condition."""

    @pytest.mark.asyncio
    async def test_not_active(self, seeded_session: AsyncSession):
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        inactive = ~active_spec
        names = await _names(seeded_session, inactive)
        assert names == ["Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_not_admin(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        non_admin = ~admin_spec
        names = await _names(seeded_session, non_admin)
        assert names == ["Bob", "Diana"]


class TestSpecificationComplex:
    """Complex compositions: (spec1 & spec2) | spec3."""

    @pytest.mark.asyncio
    async def test_complex_composition(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        diana_spec: Specification[User] = Specification(lambda root, q: q.where(root.name == "Diana"))
        # (admin AND active) OR Diana  -> Alice OR Diana
        combined = (admin_spec & active_spec) | diana_spec
        names = await _names(seeded_session, combined)
        assert names == ["Alice", "Diana"]

    @pytest.mark.asyncio
    async def test_not_combined_with_and(self, seeded_session: AsyncSession):
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        active_spec: Specification[User] = Specification(
            lambda root, q: q.where(root.active == True)  # noqa: E712
        )
        # NOT admin AND active -> non-admin active users -> Bob
        combined = ~admin_spec & active_spec
        names = await _names(seeded_session, combined)
        assert names == ["Bob"]


class TestSpecificationNoop:
    """Empty / no-op specification."""

    @pytest.mark.asyncio
    async def test_noop_returns_all(self, seeded_session: AsyncSession):
        noop: Specification[User] = Specification(lambda root, q: q)
        names = await _names(seeded_session, noop)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]

    @pytest.mark.asyncio
    async def test_noop_and_spec(self, seeded_session: AsyncSession):
        noop: Specification[User] = Specification(lambda root, q: q)
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        combined = noop & admin_spec
        names = await _names(seeded_session, combined)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_noop_or_spec(self, seeded_session: AsyncSession):
        noop: Specification[User] = Specification(lambda root, q: q)
        admin_spec: Specification[User] = Specification(lambda root, q: q.where(root.role == "admin"))
        # noop has no whereclause, so OR falls through to admin_spec
        combined = noop | admin_spec
        names = await _names(seeded_session, combined)
        assert names == ["Alice", "Charlie"]

    @pytest.mark.asyncio
    async def test_not_noop(self, seeded_session: AsyncSession):
        noop: Specification[User] = Specification(lambda root, q: q)
        # NOT noop — no clause to negate, so should return all
        negated = ~noop
        names = await _names(seeded_session, negated)
        assert names == ["Alice", "Bob", "Charlie", "Diana"]
