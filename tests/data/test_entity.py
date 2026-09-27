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
from pathlib import Path
from uuid import UUID

import pytest
from alembic.autogenerate import produce_migrations, render_python_code
from alembic.migration import MigrationContext
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import CreateTable, conv
from sqlalchemy.sql.schema import DEFAULT_NAMING_CONVENTION

from pyfly.data.page import Page
from pyfly.data.relational.sqlalchemy import UtcDateTime as ExportedUtcDateTime
from pyfly.data.relational.sqlalchemy.entity import MAX_CONSTRAINT_NAME_LENGTH, NAMING_CONVENTION, Base, BaseEntity
from pyfly.data.relational.sqlalchemy.naming import apply_convention_to_operations
from pyfly.data.relational.sqlalchemy.types import UtcDateTime, to_utc


class User(BaseEntity):
    """Concrete entity for testing."""

    __tablename__ = "users"

    name: Mapped[str] = mapped_column(String(100))


class ConventionParent(Base):
    __tablename__ = "entity_convention_parent_with_a_rather_long_table_name"

    id: Mapped[int] = mapped_column(primary_key=True)


class ConventionChild(Base):
    __tablename__ = "entity_convention_child_with_an_equally_long_table_name"
    __table_args__ = (
        UniqueConstraint("first_rather_long_column_name", "second_rather_long_column_name"),
        CheckConstraint("amount >= 0"),
        CheckConstraint("amount < 100", name="child_amount_below_100"),
        UniqueConstraint("code", name="child_code_is_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    first_rather_long_column_name: Mapped[int] = mapped_column(Integer)
    second_rather_long_column_name: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(10), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[int] = mapped_column(ForeignKey("entity_convention_parent_with_a_rather_long_table_name.id"))


class IntegerKeyedEntity(BaseEntity):
    """A BaseEntity whose key is its own sequential integer, not the random UUID."""

    __tablename__ = "entity_integer_keyed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # type: ignore[assignment]


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


class TestSqlServerColumns:
    """SQL Server gets Unicode audit columns and a non-clustered random-UUID key (C169); the other dialects'
    DDL is unchanged."""

    def test_audit_user_columns_are_nvarchar_on_sql_server(self) -> None:
        ddl = _ddl(User, mssql.dialect())
        assert "created_by NVARCHAR(255) NULL" in ddl
        assert "updated_by NVARCHAR(255) NULL" in ddl

    def test_random_uuid_key_is_not_the_clustered_index_on_sql_server(self) -> None:
        assert "CONSTRAINT pk_users PRIMARY KEY NONCLUSTERED (id)" in _ddl(User, mssql.dialect())

    def test_an_entity_with_its_own_key_keeps_the_default_clustering(self) -> None:
        ddl = _ddl(IntegerKeyedEntity, mssql.dialect())
        assert "PRIMARY KEY (id)" in ddl and "NONCLUSTERED" not in ddl

    @pytest.mark.parametrize(
        "dialect", [postgresql.dialect(), mysql.dialect(), sqlite.dialect()], ids=["postgresql", "mysql", "sqlite"]
    )
    def test_other_dialects_are_unchanged(self, dialect: object) -> None:
        ddl = _ddl(User, dialect)
        assert "created_by VARCHAR(255)" in ddl
        assert "CONSTRAINT pk_users PRIMARY KEY (id)" in ddl


class TestNamingConvention:
    """``Base`` names its tables' unnamed constraints the same way on every backend (C126); the reflected
    names, a migration that runs on every lane, a legacy history replayed and the operations opt-in are
    ``tests/integration/test_naming_convention_matrix.py``."""

    def test_alembic_operations_see_no_convention_by_default(self) -> None:
        """The models' constraints carry the names; ``Base.metadata.naming_convention``, the one Alembic
        operations apply to a revision's unnamed constraints, stays SQLAlchemy's default, so an existing
        history replays with the names it always had."""
        assert Base.metadata.naming_convention == DEFAULT_NAMING_CONVENTION
        generated = [c.name for c in ConventionChild.__table__.constraints if not str(c.name).startswith("child_")]
        assert len(generated) == 4 and all(isinstance(name, conv) for name in generated)

    def test_a_table_declared_on_the_metadata_is_named_too(self) -> None:
        link = Table(
            "entity_convention_link",
            Base.metadata,
            Column("parent_id", ForeignKey("entity_convention_parent_with_a_rather_long_table_name.id")),
            Column("tag", String(20), CheckConstraint("tag <> ''")),
            Column("position", Integer, unique=True),
            PrimaryKeyConstraint("parent_id", "tag"),
        )
        try:
            names = sorted(str(constraint.name) for constraint in [*link.constraints, *link.c.tag.constraints])
            assert names[0].startswith("ck_entity_convention_link_")
            assert names[1].startswith("fk_entity_convention_link_parent_id_entity_convention_")
            assert names[2:] == ["pk_entity_convention_link", "uq_entity_convention_link_position"]
        finally:
            Base.metadata.remove(link)

    def test_another_metadata_is_left_alone(self) -> None:
        other = MetaData()
        table = Table("entity_convention_other", other, Column("id", Integer, primary_key=True, unique=True))
        assert [constraint.name for constraint in table.constraints] == [None, None]

    def test_autogenerate_renders_the_names_explicitly(self, tmp_path: Path) -> None:
        """A revision autogenerated against ``Base.metadata`` spells every name out (``op.f()``), so it does
        not depend on the convention when it is replayed."""
        tables = {ConventionParent.__tablename__, ConventionChild.__tablename__}
        engine = create_engine(f"sqlite:///{tmp_path / 'autogenerate.db'}")
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={
                        "target_metadata": Base.metadata,
                        "include_name": lambda name, type_, _parent: type_ != "table" or name in tables,
                    },
                )
                code = render_python_code(produce_migrations(context, Base.metadata).upgrade_ops)
        finally:
            engine.dispose()
        for constraint in ConventionChild.__table__.constraints:
            generated = isinstance(constraint.name, conv) and not constraint.name.startswith("child_")
            rendered = f"name=op.f('{constraint.name}')" if generated else f"name='{constraint.name}'"
            assert rendered in code, (constraint.name, code)
        assert "op.f('ix_entity_convention_child_with_an_equally_long_table_name_code')" in code

    def test_operations_opt_in_names_as_the_models_do(self) -> None:
        opted_in = apply_convention_to_operations(MetaData())
        assert opted_in.naming_convention == NAMING_CONVENTION
        operations_names = sorted(str(constraint.name) for constraint in _operations_copy(opted_in).constraints)
        model_names = sorted(str(constraint.name) for constraint in ConventionChild.__table__.constraints)
        assert operations_names == model_names


def _operations_copy(metadata: MetaData) -> Table:
    """``ConventionChild``'s table as a revision's ``op.create_table`` declares it: every constraint unnamed
    but the two the model names."""
    return Table(
        "entity_convention_child_with_an_equally_long_table_name",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("first_rather_long_column_name", Integer),
        Column("second_rather_long_column_name", Integer),
        Column("code", String(10), index=True),
        Column("amount", Integer),
        Column("parent_id", Integer, ForeignKey("entity_convention_parent_with_a_rather_long_table_name.id")),
        UniqueConstraint("first_rather_long_column_name", "second_rather_long_column_name"),
        CheckConstraint("amount >= 0"),
        CheckConstraint("amount < 100", name="child_amount_below_100"),
        UniqueConstraint("code", name="child_code_is_unique"),
    )


class TestNamingConventionNames:
    """The names themselves."""

    def test_long_names_are_cut_with_a_hash_at_the_postgresql_limit(self) -> None:
        constraints = ConventionChild.__table__.constraints
        assert all(len(str(constraint.name)) <= MAX_CONSTRAINT_NAME_LENGTH for constraint in constraints)
        unique = next(
            str(constraint.name)
            for constraint in constraints
            if isinstance(constraint, UniqueConstraint) and len(constraint.columns) == 2
        )
        assert unique.startswith("uq_entity_convention_child")
        assert len(unique) == MAX_CONSTRAINT_NAME_LENGTH and unique[-9] == "_"

    @pytest.mark.parametrize(
        "dialect", [postgresql.dialect(), mysql.dialect(), sqlite.dialect(), mssql.dialect(), oracle.dialect()]
    )
    def test_every_dialect_renders_the_same_names(self, dialect: object) -> None:
        ddl = _ddl(ConventionChild, dialect)
        for constraint in ConventionChild.__table__.constraints:
            if isinstance(constraint.name, str) and not constraint.name.startswith("pk_"):
                assert f"CONSTRAINT {constraint.name} " in ddl, (constraint.name, ddl)

    def test_explicit_names_are_kept(self) -> None:
        names = {str(constraint.name) for constraint in ConventionChild.__table__.constraints}
        assert {"child_amount_below_100", "child_code_is_unique"} <= names

    def test_an_unnamed_check_is_named_after_its_sql(self) -> None:
        checks = [
            str(constraint.name)
            for constraint in ConventionChild.__table__.constraints
            if isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "amount >= 0"
        ]
        assert len(checks) == 1 and checks[0].startswith("ck_entity_convention_child")

    def test_primary_key_foreign_key_and_index_names(self) -> None:
        table = ConventionChild.__table__
        assert table.primary_key.name == "pk_entity_convention_child_with_an_equally_long_table_name"
        (foreign_key,) = table.foreign_key_constraints
        assert str(foreign_key.name).startswith("fk_entity_convention_child")
        assert len(str(foreign_key.name)) == MAX_CONSTRAINT_NAME_LENGTH
        (index,) = table.indexes
        assert index.name == "ix_entity_convention_child_with_an_equally_long_table_name_code"


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
