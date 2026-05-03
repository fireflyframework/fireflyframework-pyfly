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
"""Tests for BaseEntity and Page types."""

from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.page import Page
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity


class User(BaseEntity):
    """Concrete entity for testing."""

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(100))


@pytest.fixture
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(async_engine):
    async with AsyncSession(async_engine) as session:
        yield session


class TestBaseEntity:
    @pytest.mark.asyncio
    async def test_has_id(self, session: AsyncSession):
        user = User(name="Alice")
        session.add(user)
        await session.flush()
        assert isinstance(user.id, UUID)

    @pytest.mark.asyncio
    async def test_id_is_unique(self, session: AsyncSession):
        a = User(name="Alice")
        b = User(name="Bob")
        session.add_all([a, b])
        await session.flush()
        assert a.id != b.id

    @pytest.mark.asyncio
    async def test_has_audit_fields(self, session: AsyncSession):
        user = User(name="Alice")
        session.add(user)
        await session.flush()
        assert user.created_at is not None
        assert user.updated_at is not None
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)

    @pytest.mark.asyncio
    async def test_created_by_defaults_to_none(self, session: AsyncSession):
        user = User(name="Alice")
        session.add(user)
        await session.flush()
        assert user.created_by is None
        assert user.updated_by is None

    @pytest.mark.asyncio
    async def test_audit_fields_settable(self, session: AsyncSession):
        user = User(name="Alice", created_by="system", updated_by="system")
        session.add(user)
        await session.flush()
        assert user.created_by == "system"
        assert user.updated_by == "system"

    @pytest.mark.asyncio
    async def test_timestamps_are_utc(self, session: AsyncSession):
        user = User(name="Alice")
        session.add(user)
        await session.flush()
        assert user.created_at.tzinfo is not None


class TestPage:
    def test_page_creation(self):
        page = Page(items=["a", "b", "c"], total=10, page=1, size=3)
        assert page.items == ["a", "b", "c"]
        assert page.total == 10
        assert page.page == 1
        assert page.size == 3

    def test_total_pages(self):
        page = Page(items=[], total=25, page=1, size=10)
        assert page.total_pages == 3

    def test_total_pages_exact_division(self):
        page = Page(items=[], total=20, page=1, size=10)
        assert page.total_pages == 2

    def test_total_pages_empty(self):
        page = Page(items=[], total=0, page=1, size=10)
        assert page.total_pages == 0

    def test_has_next(self):
        page = Page(items=[1, 2], total=10, page=1, size=2)
        assert page.has_next is True

    def test_has_next_last_page(self):
        page = Page(items=[9, 10], total=10, page=5, size=2)
        assert page.has_next is False

    def test_has_previous(self):
        page = Page(items=[], total=10, page=2, size=5)
        assert page.has_previous is True

    def test_has_previous_first_page(self):
        page = Page(items=[], total=10, page=1, size=5)
        assert page.has_previous is False

    def test_generic_typing(self):
        page: Page[str] = Page(items=["a"], total=1, page=1, size=10)
        assert page.items[0] == "a"

    def test_map(self):
        page = Page(items=[1, 2, 3], total=3, page=1, size=10)
        mapped = page.map(str)
        assert mapped.items == ["1", "2", "3"]
        assert mapped.total == 3
