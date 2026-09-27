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

import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import ForeignKey, String, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pyfly.data.page import Page
from pyfly.data.pageable import Pageable, Sort
from pyfly.data.post_processor import is_stub
from pyfly.data.query_parser import InvalidQueryMethodError
from pyfly.data.relational.sqlalchemy import post_processor as post_processor_module
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.post_processor import RepositoryBeanPostProcessor
from pyfly.data.relational.sqlalchemy.query import query
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.types import UtcDateTime
from tests.support.backend_matrix import RelationalBackend, enable_sqlite_foreign_keys

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
async def engine(tmp_path: Path):
    """A SQLite file database with foreign keys on, holding every table of ``Base.metadata``."""
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    enable_sqlite_foreign_keys(eng)
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


# ===========================================================================
# 8. Stubs are recognized by the shape of their body (C002)
# ===========================================================================


class StubOwner(Base):
    __tablename__ = "pp_stub_owner"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    email: Mapped[str] = mapped_column(String(100))


class StubDoc(Base):
    __tablename__ = "pp_stub_doc"

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[str] = mapped_column(ForeignKey("pp_stub_owner.id"))
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True, default=None)


class HandWrittenRepo(Repository[StubDoc, str]):
    """Hand-written find_by_/count_by_/delete_by_ bodies without a single literal: none is a stub."""

    async def find_by_name(self, name: str) -> list[StubDoc]:
        result = await self._session.execute(select(StubDoc).where(func.lower(StubDoc.name) == func.lower(name)))
        return list(result.scalars().all())

    async def find_by_owner_email(self, email: str) -> list[StubDoc]:
        """A join lookup: the entity has no ``owner_email`` property."""
        statement = select(StubDoc).join(StubOwner, StubOwner.id == StubDoc.owner_id).where(StubOwner.email == email)
        return list((await self._session.execute(statement)).scalars().all())

    async def count_by_name(self, name: str) -> int:
        return len(await self.find_by_name(name))

    async def exists_by_name(self, name: str) -> bool:
        return bool(await self.find_by_name(name))

    async def delete_by_owner_id(self, owner_id: str) -> int:
        """A soft delete: the rows stay, with ``deleted_at`` set."""
        rows = (await self._session.execute(select(StubDoc).where(StubDoc.owner_id == owner_id))).scalars().all()
        for row in rows:
            row.deleted_at = datetime.now(UTC)
        await self._session.flush()
        return len(rows)


class StubShapesRepo(Repository[StubDoc, str]):
    """Every body shape that is a stub."""

    async def find_by_name(self, name: str) -> list[StubDoc]: ...

    async def count_by_name(self, name: str) -> int:
        pass

    async def exists_by_name(self, name: str) -> bool:
        """Documented, and no body at all."""

    async def find_by_owner_id(self, owner_id: str) -> list[StubDoc]:
        """Documented stub."""
        ...

    async def count_by_owner_id(self, owner_id: str) -> int:
        raise NotImplementedError

    async def exists_by_owner_id(self, owner_id: str) -> bool:
        raise NotImplementedError()

    async def find_by_id_in(self, ids: list[str]) -> list[StubDoc]:
        raise NotImplementedError("derived")


class StubBase(Repository[StubDoc, str]):
    """An intermediate base: its stubs are compiled for every repository that extends it."""

    async def find_by_name(self, name: str) -> list[StubDoc]: ...

    async def count_by_name(self, name: str) -> int: ...


class InheritingRepo(StubBase):
    """Inherits find_by_name as a stub, and overrides count_by_name with a real body."""

    async def count_by_name(self, name: str) -> int:
        return len(await self.find_by_name(name)) * 100


@pytest.fixture
async def file_session(relational_backend: RelationalBackend) -> AsyncIterator[AsyncSession]:
    """A session on the sqlite-file lane (foreign keys on), with two owners and three documents."""
    await relational_backend.create_tables(StubOwner, StubDoc)
    engine = relational_backend.create_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([StubOwner(id="o1", email="alice@example.com"), StubOwner(id="o2", email="bob@example.com")])
        await session.flush()
        session.add_all(
            [
                StubDoc(id="d1", name="MixedCase", owner_id="o1"),
                StubDoc(id="d2", name="other", owner_id="o1"),
                StubDoc(id="d3", name="other", owner_id="o2"),
            ]
        )
        await session.flush()
        yield session


@pytest.mark.backends("sqlite-file")
class TestStubDetection:
    """A body with real code is never replaced, however little it holds; every stub shape is compiled."""

    async def test_hand_written_bodies_without_literals_survive(
        self, processor: RepositoryBeanPostProcessor, file_session: AsyncSession
    ):
        repo = HandWrittenRepo(StubDoc, file_session)
        processor.after_init(repo, "handWritten")

        assert not {
            "find_by_name",
            "find_by_owner_email",
            "count_by_name",
            "exists_by_name",
            "delete_by_owner_id",
        } & set(vars(repo))
        assert [doc.id for doc in await repo.find_by_name("mixedcase")] == ["d1"]
        assert sorted(doc.id for doc in await repo.find_by_owner_email("alice@example.com")) == ["d1", "d2"]
        assert await repo.count_by_name("OTHER") == 2
        assert await repo.exists_by_name("MIXEDCASE") is True

    async def test_a_hand_written_soft_delete_keeps_its_rows(
        self, processor: RepositoryBeanPostProcessor, file_session: AsyncSession
    ):
        repo = HandWrittenRepo(StubDoc, file_session)
        processor.after_init(repo, "handWritten")

        assert await repo.delete_by_owner_id("o1") == 2
        rows = (await file_session.execute(text("SELECT id, deleted_at FROM pp_stub_doc ORDER BY id"))).all()
        assert [(row[0], row[1] is not None) for row in rows] == [("d1", True), ("d2", True), ("d3", False)]

    async def test_every_stub_shape_is_compiled(
        self, processor: RepositoryBeanPostProcessor, file_session: AsyncSession
    ):
        repo = StubShapesRepo(StubDoc, file_session)
        processor.after_init(repo, "stubShapes")

        assert {
            "find_by_name",
            "count_by_name",
            "exists_by_name",
            "find_by_owner_id",
            "count_by_owner_id",
            "exists_by_owner_id",
            "find_by_id_in",
        } <= set(vars(repo))
        assert [doc.id for doc in await repo.find_by_name("other")] in (["d2", "d3"], ["d3", "d2"])
        assert await repo.count_by_name("other") == 2
        assert await repo.exists_by_name("MixedCase") is True
        assert sorted(doc.id for doc in await repo.find_by_owner_id("o1")) == ["d1", "d2"]
        assert await repo.count_by_owner_id("o2") == 1
        assert await repo.exists_by_owner_id("o3") is False
        assert sorted(doc.id for doc in await repo.find_by_id_in(["d1", "d3"])) == ["d1", "d3"]

    async def test_stubs_of_an_intermediate_base_are_compiled(
        self, processor: RepositoryBeanPostProcessor, file_session: AsyncSession
    ):
        repo = InheritingRepo(StubDoc, file_session)
        processor.after_init(repo, "inheriting")

        assert sorted(doc.id for doc in await repo.find_by_name("other")) == ["d2", "d3"]
        # The subclass's real override of the base's stub is kept.
        assert "count_by_name" not in vars(repo)
        assert await repo.count_by_name("other") == 200


class SignatureShapesRepo(Repository[StubDoc, str]):
    async def find_by_owner_id_and_name(self, owner_id: str, *, name: str) -> list[StubDoc]: ...

    async def count_by_name_or_owner_id(self, *values: str) -> int: ...

    async def exists_by_name(self, name: str = "other") -> bool: ...


@pytest.mark.backends("sqlite-file")
class TestArgumentBinding:
    """A compiled method takes its arguments as the stub declares them."""

    async def test_keyword_only_variadic_and_default_parameters(
        self, processor: RepositoryBeanPostProcessor, file_session: AsyncSession
    ):
        repo = processor.after_init(SignatureShapesRepo(StubDoc, file_session), "signatures")
        assert [doc.id for doc in await repo.find_by_owner_id_and_name("o1", name="other")] == ["d2"]
        with pytest.raises(TypeError, match="find_by_owner_id_and_name"):
            await repo.find_by_owner_id_and_name("o1", "other")
        assert await repo.count_by_name_or_owner_id("MixedCase", "o2") == 2
        assert await repo.exists_by_name() is True
        assert await repo.exists_by_name("nothing") is False


class TestIsStub:
    """``is_stub`` reads the body's shape, whatever the Python version compiles it to."""

    def test_stub_shapes(self):
        async def ellipsis(self, x): ...

        async def documented(self, x):
            """Doc."""

        def sync_pass(self, x):
            pass

        async def raises(self, x):
            raise NotImplementedError

        async def raises_with_message(self, x):
            raise NotImplementedError("not yet")

        for function in (ellipsis, documented, sync_pass, raises, raises_with_message):
            assert is_stub(function), function.__name__

    def test_real_bodies(self):
        async def delegating(self, x):
            return await self.find_all_by_spec(x)

        async def returns_argument(self, x):
            return x

        async def returns_literal(self, x):
            return "x"

        async def raises_something_else(self, x):
            raise ValueError

        async def documented_and_real(self, x):
            """Doc."""
            return await self.find_all_by_spec(x)

        for function in (delegating, returns_argument, returns_literal, raises_something_else, documented_and_real):
            assert not is_stub(function), function.__name__

    def test_a_wrapped_stub_is_read_through_its_wrappers(self):
        async def stub(self, x): ...

        @functools.wraps(stub)
        async def wrapper(self, x):
            return await stub(self, x)

        assert is_stub(wrapper)
        assert not is_stub(len)


# ===========================================================================
# 9. Derived methods are checked against their entity when the repository is built (C128)
# ===========================================================================


class CheckedItem(Base):
    __tablename__ = "pp_checked_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(40))
    tag: Mapped[str] = mapped_column(String(20))
    balance: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("pp_stub_owner.id"), nullable=True)
    owner: Mapped[StubOwner | None] = relationship()

    @property
    def label(self) -> str:
        return f"{self.name}/{self.tag}"


class _Typo(Repository[CheckedItem, int]):
    async def find_by_nmae(self, name: str) -> list[CheckedItem]: ...


class _NotAProperty(Repository[CheckedItem, int]):
    async def find_by_label(self, label: str) -> list[CheckedItem]: ...


class _MissingArgument(Repository[CheckedItem, int]):
    async def delete_by_tag(self, tag: str, item_id: int) -> int: ...


class _MissingParameter(Repository[CheckedItem, int]):
    async def find_by_balance_between(self, low: int) -> list[CheckedItem]: ...


class _PageWithoutPageable(Repository[CheckedItem, int]):
    async def find_by_tag(self, tag: str) -> Page[CheckedItem]: ...


class _PageableOnASingleResult(Repository[CheckedItem, int]):
    async def find_by_tag(self, tag: str, pageable: Pageable) -> CheckedItem | None: ...


class _FoldedNumber(Repository[CheckedItem, int]):
    async def find_by_balance_ignore_case(self, balance: int) -> list[CheckedItem]: ...


class _TrueOnAString(Repository[CheckedItem, int]):
    async def find_by_name_true(self) -> list[CheckedItem]: ...


class _RelationshipIn(Repository[CheckedItem, int]):
    async def find_by_owner_in(self, owners: list[StubOwner]) -> list[CheckedItem]: ...


class _CountAsList(Repository[CheckedItem, int]):
    async def count_by_tag(self, tag: str) -> list[CheckedItem]: ...


class _ExistsAsInt(Repository[CheckedItem, int]):
    async def exists_by_tag(self, tag: str) -> int: ...


class _DeleteAsString(Repository[CheckedItem, int]):
    async def delete_by_tag(self, tag: str) -> str: ...


class _FindScalars(Repository[CheckedItem, int]):
    async def find_by_tag(self, tag: str) -> list[int]: ...


class _TwoResultTypes(Repository[CheckedItem, int]):
    async def find_by_tag(self, tag: str) -> int | str: ...


class _UnresolvedAnnotation(Repository[CheckedItem, int]):
    async def find_by_tag(self, tag: str) -> list[Missing]: ...  # type: ignore[name-defined]  # noqa: F821


class _OrderByUnknown(Repository[CheckedItem, int]):
    async def find_by_tag_order_by_label(self, tag: str) -> list[CheckedItem]: ...


class _BadQuery(Repository[CheckedItem, int]):
    @query("SELECT c FROM CheckedItem c WHERE c.nmae = :name")
    async def by_name(self, name: str) -> list[CheckedItem]: ...


class _Valid(Repository[CheckedItem, int]):
    """Everything that is checked, done right."""

    async def find_by_tag_and_balance_between(self, tag: str, low: int, high: int) -> list[CheckedItem]: ...

    async def find_by_name_ignore_case(self, name: str) -> CheckedItem | None: ...

    async def find_by_active_true_order_by_balance_desc(self, pageable: Pageable) -> Page[CheckedItem]: ...

    async def find_by_owner(self, owner: StubOwner | None) -> list[CheckedItem]: ...

    async def find_by_tag_in(self, tags: list[str], sort: Sort) -> list[CheckedItem]: ...

    async def count_by_tag(self, tag: str) -> int: ...

    async def exists_by_tag(self, tag: str) -> bool: ...

    async def delete_by_tag(self, tag: str) -> None: ...


class TestDerivedMethodsAreCheckedAtStartup:
    """A derived method that cannot work fails when the post-processor builds the repository, not on its first
    call (Spring rejects such a method at bootstrap)."""

    @pytest.mark.parametrize(
        ("repository_type", "message"),
        [
            (_Typo, "'nmae' names no property"),
            (_NotAProperty, "'label' names no property"),
            (_MissingArgument, "takes 1 argument .* declares 2 value parameters"),
            (_MissingParameter, "takes 2 arguments .* declares 1 value parameter"),
            (_PageWithoutPageable, "needs a Pageable"),
            (_PageableOnASingleResult, "several entities takes a Pageable"),
            (_FoldedNumber, "cannot ignore case"),
            (_TrueOnAString, "not a boolean property"),
            (_RelationshipIn, "is a relationship"),
            (_CountAsList, "count_by method returns int"),
            (_ExistsAsInt, "exists_by method returns bool"),
            (_DeleteAsString, "delete_by method returns int"),
            (_FindScalars, "find_by method returns CheckedItem entities"),
            (_TwoResultTypes, "one type"),
            (_UnresolvedAnnotation, "annotations do not resolve"),
            (_OrderByUnknown, "order_by_label"),
            (_BadQuery, "no attribute or column 'nmae'"),
        ],
    )
    def test_the_repository_fails_to_build(
        self, processor: RepositoryBeanPostProcessor, repository_type: type[Repository[CheckedItem, int]], message: str
    ):
        with pytest.raises(InvalidQueryMethodError, match=message) as raised:
            processor.after_init(repository_type(CheckedItem), repository_type.__name__)
        assert repository_type.__name__ in str(raised.value)

    def test_a_valid_repository_builds(self, processor: RepositoryBeanPostProcessor):
        repository = processor.after_init(_Valid(CheckedItem), "valid")
        compiled = {name for name in vars(repository) if not name.startswith("_")}
        assert compiled == {name for name in vars(_Valid) if not name.startswith("_")}

    def test_a_transient_repository_compiles_its_methods_once(self, processor: RepositoryBeanPostProcessor):
        first = processor.after_init(_Valid(CheckedItem), "first")
        second = RepositoryBeanPostProcessor().after_init(_Valid(CheckedItem), "second")
        assert first.count_by_tag is not second.count_by_tag
        query_of = post_processor_module._COMPILED[_Valid]
        assert (CheckedItem, "count_by_tag") in query_of
