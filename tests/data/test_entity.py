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
"""Tests for BaseEntity, UtcDateTime and Page types.

The timestamp contract on every backend (reload, UTC, microseconds, offset parameters) is
``tests/integration/test_entity_timestamps_matrix.py``; these tests pin the type itself.
"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import String
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from pyfly.data.page import Page
from pyfly.data.relational.sqlalchemy import UtcDateTime as ExportedUtcDateTime
from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity
from pyfly.data.relational.sqlalchemy.types import UtcDateTime, to_utc


class User(BaseEntity):
    """Concrete entity for testing."""

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(100))


class StrictReading(Base):
    """A strict ``UtcDateTime`` column: naive values are rejected."""

    __tablename__ = "entity_strict_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    taken_at: Mapped[datetime] = mapped_column(UtcDateTime(strict=True))


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
    async def test_timestamps_reload_as_aware_utc(self, session: AsyncSession):
        """The value read back from the database, not the in-memory default (C178): SQLite returned it
        naive, so ``created_at < datetime.now(UTC)`` raised ``TypeError`` after a reload."""
        user = User(name="Alice")
        session.add(user)
        await session.flush()
        stamped, user_id = user.created_at, user.id
        await session.commit()
        session.expunge_all()

        reloaded = await session.get(User, user_id)

        assert reloaded is not None
        assert reloaded.created_at.utcoffset() == timedelta(0)
        assert reloaded.updated_at.utcoffset() == timedelta(0)
        assert reloaded.created_at == stamped
        assert reloaded.created_at <= datetime.now(UTC)


def _ddl(model: type, dialect: object) -> str:
    return str(CreateTable(model.__table__).compile(dialect=dialect))  # type: ignore[attr-defined]


class TestUtcDateTime:
    def test_exported_from_the_sqlalchemy_package(self) -> None:
        assert ExportedUtcDateTime is UtcDateTime

    @pytest.mark.parametrize(
        ("dialect", "ddl"),
        [
            (postgresql.dialect(), "created_at TIMESTAMP WITH TIME ZONE NOT NULL"),
            (sqlite.dialect(), "created_at DATETIME NOT NULL"),
            (mysql.dialect(), "created_at DATETIME(6) NOT NULL"),
            (mysql.dialect(is_mariadb=True), "created_at DATETIME(6) NOT NULL"),
            (mssql.dialect(), "created_at DATETIMEOFFSET NOT NULL"),
            (oracle.dialect(), "created_at TIMESTAMP WITH TIME ZONE NOT NULL"),
        ],
        ids=["postgresql", "sqlite", "mysql", "mariadb", "mssql", "oracle"],
    )
    def test_column_type_per_dialect(self, dialect: object, ddl: str) -> None:
        """Microseconds on MySQL/MariaDB (``DATETIME`` had none), the offset kept where the backend has a
        type for it (Oracle got a ``DATE`` before), and unchanged DDL on PostgreSQL and SQLite."""
        assert ddl in _ddl(User, dialect)

    def test_bind_converts_to_utc_and_strips_the_offset_where_the_column_has_none(self) -> None:
        plus_two = datetime(2026, 9, 24, 12, 0, 0, 5, tzinfo=timezone(timedelta(hours=2)))
        column_type = UtcDateTime()
        assert column_type.process_bind_param(plus_two, sqlite.dialect()) == datetime(2026, 9, 24, 10, 0, 0, 5)
        assert column_type.process_bind_param(plus_two, mysql.dialect()) == datetime(2026, 9, 24, 10, 0, 0, 5)
        on_pg = column_type.process_bind_param(plus_two, postgresql.dialect())
        assert on_pg == plus_two and on_pg.tzinfo is UTC

    def test_result_attaches_utc(self) -> None:
        loaded = UtcDateTime().process_result_value(datetime(2026, 9, 24, 10, 0), sqlite.dialect())
        assert loaded == datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

    def test_to_utc(self) -> None:
        assert to_utc(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        assert to_utc(datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))) == datetime(
            2026, 1, 1, 17, 0, tzinfo=UTC
        )
        with pytest.raises(ValueError, match="naive datetime"):
            to_utc(datetime(2026, 1, 1, 12, 0), strict=True)

    @pytest.mark.asyncio
    async def test_strict_column_rejects_a_naive_value(self, session: AsyncSession) -> None:
        session.add(StrictReading(id=1, taken_at=datetime(2026, 9, 24, 10, 0)))
        with pytest.raises(StatementError, match="naive datetime"):
            await session.flush()

    @pytest.mark.asyncio
    async def test_strict_column_stores_an_aware_value(self, session: AsyncSession) -> None:
        taken = datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        session.add(StrictReading(id=2, taken_at=taken))
        await session.commit()
        session.expunge_all()
        reloaded = await session.get(StrictReading, 2)
        assert reloaded is not None and reloaded.taken_at == taken and reloaded.taken_at.tzinfo is UTC

    def test_repr_renders_for_migrations(self) -> None:
        assert repr(UtcDateTime()) == "UtcDateTime()"
        assert repr(UtcDateTime(strict=True)) == "UtcDateTime(strict=True)"


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
