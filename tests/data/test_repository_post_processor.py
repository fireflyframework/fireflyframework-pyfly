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
"""Tests for RepositoryBeanPostProcessor."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.query import query
from pyfly.data.relational.sqlalchemy.repository import Repository

# ---------------------------------------------------------------------------
# Test entity
# ---------------------------------------------------------------------------


class PPItem(BaseEntity):
    __tablename__ = "pp_test_items"

    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(50), default="user")
    active: Mapped[bool] = mapped_column(default=True)


# ---------------------------------------------------------------------------
# Test repositories
# ---------------------------------------------------------------------------


class QueryDecoratedRepo(Repository[PPItem, UUID]):
    """Repository with only @query-decorated methods."""

    @query("SELECT * FROM pp_test_items WHERE role = :role", native=True)
    async def find_by_role_query(self, role: str) -> list[PPItem]: ...


class DerivedQueryRepo(Repository[PPItem, UUID]):
    """Repository with only derived query methods."""

    async def find_by_name(self, name: str) -> list[PPItem]: ...

    async def find_by_active(self, active: bool) -> list[PPItem]: ...


class MixedRepo(Repository[PPItem, UUID]):
    """Repository with both @query-decorated and derived query methods."""

    @query("SELECT * FROM pp_test_items WHERE role = :role", native=True)
    async def find_by_role_query(self, role: str) -> list[PPItem]: ...

    async def find_by_name(self, name: str) -> list[PPItem]: ...

    async def find_by_active(self, active: bool) -> list[PPItem]: ...


class ConcreteMethodRepo(Repository[PPItem, UUID]):
    """Repository with a concrete (non-stub) method that starts with find_by_."""

    async def find_by_name(self, name: str) -> list[PPItem]:
        # This is a concrete implementation, NOT a stub.
        result = await self._session.execute(__import__("sqlalchemy").select(PPItem).where(PPItem.name == name))
        return list(result.scalars().all())


class AllDerivedTypesRepo(Repository[PPItem, UUID]):
    """Repository with all four derived query prefixes."""

    async def find_by_name(self, name: str) -> list[PPItem]: ...

    async def count_by_active(self, active: bool) -> int: ...

    async def exists_by_role(self, role: str) -> bool: ...

    async def delete_by_name(self, name: str) -> int: ...


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as sess:
        yield sess


@pytest.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    """Seed the database with known test data."""
    entities = [
        PPItem(name="Alice", role="admin", active=True),
        PPItem(name="Bob", role="user", active=True),
        PPItem(name="Carol", role="admin", active=False),
        PPItem(name="Dave", role="user", active=False),
    ]
    session.add_all(entities)
    await session.flush()
    return session


@pytest.fixture
def processor():
    return RepositoryBeanPostProcessor()


# ===========================================================================
# 1. Non-repository beans pass through unchanged
# ===========================================================================


class TestNonRepositoryPassThrough:
    """Non-repository beans should not be altered."""

    def test_plain_object_passes_through(self, processor: RepositoryBeanPostProcessor):
        """1a. A plain object is returned unchanged."""

        class PlainBean:
            value = 42

        bean = PlainBean()
        result = processor.after_init(bean, "plainBean")
        assert result is bean
        assert result.value == 42

    def test_before_init_returns_bean_unchanged(self, processor: RepositoryBeanPostProcessor):
        """1b. before_init always returns the bean unchanged."""

        class AnyBean:
            pass

        bean = AnyBean()
        result = processor.before_init(bean, "anyBean")
        assert result is bean

    def test_string_bean_passes_through(self, processor: RepositoryBeanPostProcessor):
        """1c. Primitive/string beans pass through."""
        result = processor.after_init("hello", "stringBean")
        assert result == "hello"


# ===========================================================================
# 2. @query-decorated methods get wired and work
# ===========================================================================


class TestQueryDecoratedMethods:
    """@query-decorated methods are compiled and wired onto the bean."""

    @pytest.mark.asyncio
    async def test_query_method_is_wired(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """2a. @query method returns correct results after wiring."""
        repo = QueryDecoratedRepo(PPItem, seeded_session)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_by_role_query(role="admin")
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Carol"]

    @pytest.mark.asyncio
    async def test_query_method_with_no_matches(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """2b. @query method returns empty list when no matches."""
        repo = QueryDecoratedRepo(PPItem, seeded_session)
        processor.after_init(repo, "queryRepo")

        results = await repo.find_by_role_query(role="nonexistent")
        assert results == []


# ===========================================================================
# 3. Derived query methods get wired and work
# ===========================================================================


class TestDerivedQueryMethods:
    """Derived query methods are parsed, compiled, and wired onto the bean."""

    @pytest.mark.asyncio
    async def test_find_by_name_works(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """3a. find_by_name derived query returns correct results."""
        repo = DerivedQueryRepo(PPItem, seeded_session)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_name("Alice")
        assert len(results) == 1
        assert results[0].name == "Alice"

    @pytest.mark.asyncio
    async def test_find_by_active_works(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """3b. find_by_active derived query returns correct results."""
        repo = DerivedQueryRepo(PPItem, seeded_session)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_active(True)
        names = sorted(r.name for r in results)
        assert names == ["Alice", "Bob"]

    @pytest.mark.asyncio
    async def test_derived_query_no_matches(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """3c. Derived query returns empty list when no matches."""
        repo = DerivedQueryRepo(PPItem, seeded_session)
        processor.after_init(repo, "derivedRepo")

        results = await repo.find_by_name("Nonexistent")
        assert results == []


# ===========================================================================
# 4. Both @query and derived query methods can coexist
# ===========================================================================


class TestMixedMethods:
    """Both @query and derived query methods work on the same repository."""

    @pytest.mark.asyncio
    async def test_query_and_derived_coexist(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """4a. Both @query and derived methods work on the same repo."""
        repo = MixedRepo(PPItem, seeded_session)
        processor.after_init(repo, "mixedRepo")

        # @query method
        by_role = await repo.find_by_role_query(role="admin")
        role_names = sorted(r.name for r in by_role)
        assert role_names == ["Alice", "Carol"]

        # Derived method
        by_name = await repo.find_by_name("Bob")
        assert len(by_name) == 1
        assert by_name[0].name == "Bob"

        # Another derived method
        by_active = await repo.find_by_active(False)
        inactive_names = sorted(r.name for r in by_active)
        assert inactive_names == ["Carol", "Dave"]


# ===========================================================================
# 5. Base Repository methods are NOT replaced
# ===========================================================================


class TestBaseMethodsPreserved:
    """The post-processor must not replace methods defined on Repository base."""

    @pytest.mark.asyncio
    async def test_save_still_works(
        self,
        processor: RepositoryBeanPostProcessor,
        session: AsyncSession,
    ):
        """5a. Repository.save is not replaced by the post-processor."""
        repo = MixedRepo(PPItem, session)
        processor.after_init(repo, "mixedRepo")

        entity = PPItem(name="NewEntity", role="tester", active=True)
        saved = await repo.save(entity)
        assert saved.name == "NewEntity"
        assert saved.id is not None

    @pytest.mark.asyncio
    async def test_find_by_id_still_works(
        self,
        processor: RepositoryBeanPostProcessor,
        session: AsyncSession,
    ):
        """5b. Repository.find_by_id is not replaced by the post-processor."""
        repo = MixedRepo(PPItem, session)
        processor.after_init(repo, "mixedRepo")

        entity = PPItem(name="Findable", role="user", active=True)
        saved = await repo.save(entity)

        found = await repo.find_by_id(saved.id)
        assert found is not None
        assert found.name == "Findable"

    @pytest.mark.asyncio
    async def test_find_all_still_works(
        self,
        processor: RepositoryBeanPostProcessor,
        session: AsyncSession,
    ):
        """5c. Repository.find_all is not replaced by the post-processor."""
        repo = MixedRepo(PPItem, session)
        processor.after_init(repo, "mixedRepo")

        await repo.save(PPItem(name="X", role="user"))
        await repo.save(PPItem(name="Y", role="user"))

        items = await repo.find_all()
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_count_still_works(
        self,
        processor: RepositoryBeanPostProcessor,
        session: AsyncSession,
    ):
        """5d. Repository.count is not replaced by the post-processor."""
        repo = MixedRepo(PPItem, session)
        processor.after_init(repo, "mixedRepo")

        await repo.save(PPItem(name="A", role="user"))
        total = await repo.count()
        assert total == 1


# ===========================================================================
# 6. All derived query types work (count_by_, exists_by_, delete_by_)
# ===========================================================================


class TestAllDerivedQueryTypes:
    """All four derived query prefixes must be wired correctly."""

    @pytest.mark.asyncio
    async def test_count_by_wired(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """6a. count_by_ returns correct count."""
        repo = AllDerivedTypesRepo(PPItem, seeded_session)
        processor.after_init(repo, "allTypesRepo")

        count = await repo.count_by_active(True)
        assert count == 2  # Alice and Bob

    @pytest.mark.asyncio
    async def test_exists_by_wired(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """6b. exists_by_ returns correct bool."""
        repo = AllDerivedTypesRepo(PPItem, seeded_session)
        processor.after_init(repo, "allTypesRepo")

        assert await repo.exists_by_role("admin") is True
        assert await repo.exists_by_role("superuser") is False

    @pytest.mark.asyncio
    async def test_delete_by_wired(
        self,
        processor: RepositoryBeanPostProcessor,
        session: AsyncSession,
    ):
        """6c. delete_by_ deletes and returns row count."""
        session.add_all(
            [
                PPItem(name="DeleteMe", role="temp", active=True),
                PPItem(name="KeepMe", role="perm", active=True),
            ]
        )
        await session.flush()

        repo = AllDerivedTypesRepo(PPItem, session)
        processor.after_init(repo, "allTypesRepo")

        deleted_count = await repo.delete_by_name("DeleteMe")
        assert deleted_count == 1


# ===========================================================================
# 7. Concrete methods are NOT replaced
# ===========================================================================


class TestConcreteMethodsPreserved:
    """Concrete implementations of find_by_ methods must not be replaced."""

    @pytest.mark.asyncio
    async def test_concrete_find_by_not_replaced(
        self,
        processor: RepositoryBeanPostProcessor,
        seeded_session: AsyncSession,
    ):
        """7a. Concrete find_by_ implementations are preserved."""
        repo = ConcreteMethodRepo(PPItem, seeded_session)
        processor.after_init(repo, "concreteRepo")

        results = await repo.find_by_name("Alice")
        assert len(results) == 1
        assert results[0].name == "Alice"
