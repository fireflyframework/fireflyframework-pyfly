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
"""Tests for VersionedMixin and optimistic locking."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import ForeignKey, String, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data.relational.sqlalchemy.entity import Base, BaseEntity, VersionedMixin
from pyfly.data.relational.sqlalchemy.repository import Repository


class VersionedOrder(BaseEntity, VersionedMixin):
    __tablename__ = "versioned_orders"

    name: Mapped[str] = mapped_column(String(255))


# An entity's own __mapper_args__ used to replace the mixin's, which switched optimistic locking off
# silently (C123): no version check, no increment, lost updates.


class VersionedWithOwnArgs(VersionedMixin, BaseEntity):
    __tablename__ = "versioned_own_args"
    __mapper_args__ = {"eager_defaults": True}

    name: Mapped[str] = mapped_column(String(255))


class VersionedMixinLast(BaseEntity, VersionedMixin):
    __tablename__ = "versioned_mixin_last"
    __mapper_args__ = {"eager_defaults": True}

    name: Mapped[str] = mapped_column(String(255))


class VersionedPayment(BaseEntity, VersionedMixin):
    """A polymorphic root: ``polymorphic_on`` has to be declared in the entity's own mapper args."""

    __tablename__ = "versioned_payments"
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "payment"}

    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(default=0)


class VersionedCardPayment(VersionedPayment):
    __mapper_args__ = {"polymorphic_identity": "card"}


class VersionedDocument(BaseEntity, VersionedMixin):
    __tablename__ = "versioned_documents"

    kind: Mapped[str] = mapped_column(String(20))

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"polymorphic_on": cls.kind, "polymorphic_identity": "document"}


class VersionedAttachment(VersionedDocument):
    __tablename__ = "versioned_attachments"
    __mapper_args__ = {"polymorphic_identity": "attachment"}

    id: Mapped[UUID] = mapped_column(ForeignKey("versioned_documents.id"), primary_key=True)


class EagerBase(BaseEntity):
    """An application's abstract base that declares mapper args for all its entities."""

    __abstract__ = True
    __mapper_args__ = {"eager_defaults": True}


class VersionedAfterAbstractArgs(EagerBase, VersionedMixin):
    """The abstract base's args used to shadow the mixin's: no version column at all."""

    __tablename__ = "versioned_after_abstract_args"

    name: Mapped[str] = mapped_column(String(255))


class VersionedBeforeAbstractArgs(VersionedMixin, EagerBase):
    """The mixin's args used to shadow the abstract base's: eager_defaults silently lost."""

    __tablename__ = "versioned_before_abstract_args"

    name: Mapped[str] = mapped_column(String(255))


class VersionedAbstractBase(VersionedMixin, BaseEntity):
    __abstract__ = True
    __mapper_args__ = {"eager_defaults": True}


class VersionedThroughAbstractBase(VersionedAbstractBase):
    __tablename__ = "versioned_through_abstract_base"

    name: Mapped[str] = mapped_column(String(255))


class PolymorphicMixin:
    """A mixin whose ``declared_attr`` args name the class's own column."""

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
        return {"polymorphic_on": cls.kind, "polymorphic_identity": cls.__name__.lower()}  # type: ignore[attr-defined]


class VersionedWithOwnAndInheritedArgs(PolymorphicMixin, EagerBase, VersionedMixin):
    """Args from a mixin's directive, an abstract base and the class itself are all merged, the class's
    own winning."""

    __tablename__ = "versioned_own_and_inherited_args"
    __mapper_args__ = {"eager_defaults": False}

    kind: Mapped[str] = mapped_column(String(40))


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
def repo(session):
    return Repository(VersionedOrder, session)


class TestVersionedMixin:
    @pytest.mark.asyncio
    async def test_version_starts_at_one_after_insert(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="New"))
        assert order.version == 1

    @pytest.mark.asyncio
    async def test_version_increments_on_update(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="Original"))
        assert order.version == 1

        order.name = "Updated"
        await session.flush()
        await session.refresh(order)
        assert order.version == 2

    @pytest.mark.asyncio
    async def test_version_increments_again(
        self,
        repo: Repository[VersionedOrder, UUID],
        session: AsyncSession,
    ):
        order = await repo.save(VersionedOrder(name="V1"))
        assert order.version == 1

        order.name = "V2"
        await session.flush()
        await session.refresh(order)
        assert order.version == 2

        order.name = "V3"
        await session.flush()
        await session.refresh(order)
        assert order.version == 3

    @pytest.mark.asyncio
    async def test_stale_version_raises_error(self, session_factory):
        async with session_factory() as s1:
            order = VersionedOrder(name="Shared")
            s1.add(order)
            await s1.commit()
            order_id = order.id

        async with session_factory() as s1:
            result1 = await s1.get(VersionedOrder, order_id)
            assert result1 is not None
            result1.name = "Updated by S1"

            async with session_factory() as s2:
                result2 = await s2.get(VersionedOrder, order_id)
                assert result2 is not None
                result2.name = "Updated by S2"
                await s2.commit()

            with pytest.raises(StaleDataError):
                await s1.commit()

    @pytest.mark.asyncio
    async def test_version_column_exists(self, session: AsyncSession):
        order = VersionedOrder(name="Test")
        session.add(order)
        await session.flush()
        assert hasattr(order, "version")
        assert isinstance(order.version, int)


class TestVersionedMixinWithOwnMapperArgs:
    @pytest.mark.parametrize(
        ("model", "table"),
        [
            (VersionedWithOwnArgs, "versioned_own_args"),
            (VersionedMixinLast, "versioned_mixin_last"),
            (VersionedPayment, "versioned_payments"),
            (VersionedCardPayment, "versioned_payments"),
            (VersionedDocument, "versioned_documents"),
            (VersionedAttachment, "versioned_documents"),
            (VersionedAfterAbstractArgs, "versioned_after_abstract_args"),
            (VersionedBeforeAbstractArgs, "versioned_before_abstract_args"),
            (VersionedThroughAbstractBase, "versioned_through_abstract_base"),
            (VersionedWithOwnAndInheritedArgs, "versioned_own_and_inherited_args"),
        ],
        ids=[
            "own-args",
            "mixin-last",
            "polymorphic-root",
            "single-table-subclass",
            "directive",
            "joined-subclass",
            "abstract-base-args-then-mixin",
            "mixin-then-abstract-base-args",
            "versioned-abstract-base-args",
            "mixin-directive-base-and-own-args",
        ],
    )
    def test_version_column_is_the_version_id(self, model: type, table: str) -> None:
        mapper = inspect(model)
        assert mapper.version_id_col is not None
        assert (mapper.version_id_col.table.name, mapper.version_id_col.name) == (table, "version")

    def test_the_entity_s_own_mapper_args_are_kept(self) -> None:
        assert inspect(VersionedWithOwnArgs).eager_defaults is True
        assert inspect(VersionedMixinLast).eager_defaults is True
        assert inspect(VersionedCardPayment).polymorphic_identity == "card"
        assert inspect(VersionedAttachment).polymorphic_identity == "attachment"

    @pytest.mark.parametrize(
        "model",
        [VersionedAfterAbstractArgs, VersionedBeforeAbstractArgs, VersionedThroughAbstractBase],
        ids=["abstract-base-args-then-mixin", "mixin-then-abstract-base-args", "versioned-abstract-base-args"],
    )
    def test_an_abstract_base_s_mapper_args_are_kept_whatever_the_order(self, model: type) -> None:
        assert inspect(model).eager_defaults is True

    def test_args_from_every_base_are_merged_and_the_class_s_own_win(self) -> None:
        mapper = inspect(VersionedWithOwnAndInheritedArgs)
        assert mapper.eager_defaults is False
        assert mapper.polymorphic_identity == "versionedwithownandinheritedargs"
        assert mapper.polymorphic_on is not None and mapper.polymorphic_on.name == "kind"

    def test_a_conflicting_version_id_col_in_an_abstract_base_fails_at_mapping(self) -> None:
        class _OwnVersion(BaseEntity):
            __abstract__ = True
            revision: Mapped[int] = mapped_column(default=0)

            @declared_attr.directive
            def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805
                return {"version_id_col": cls.revision}

        with pytest.raises(TypeError, match="VersionedMixin"):

            class _Conflicting(_OwnVersion, VersionedMixin):
                __tablename__ = "versioned_conflicting_abstract"

    def test_a_conflicting_version_id_col_fails_at_mapping(self) -> None:
        with pytest.raises(TypeError, match="VersionedMixin"):

            class _Conflicting(BaseEntity, VersionedMixin):
                __tablename__ = "versioned_conflicting"
                other: Mapped[int] = mapped_column(default=0)
                __mapper_args__ = {"version_id_col": other}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [VersionedWithOwnArgs, VersionedCardPayment, VersionedAfterAbstractArgs, VersionedBeforeAbstractArgs],
        ids=["own-args", "polymorphic", "abstract-base-args-then-mixin", "mixin-then-abstract-base-args"],
    )
    async def test_a_stale_write_is_rejected(self, model: type, tmp_path: Path) -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'versioned.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all, tables=[model.__table__])  # type: ignore[attr-defined]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            fields = {"amount": 1} if model is VersionedCardPayment else {"name": "v1"}
            async with factory() as session, session.begin():
                entity = model(**fields)
                session.add(entity)
            assert entity.version == 1

            async with factory() as first, factory() as second:
                stale = await first.get(model, entity.id)
                fresh = await second.get(model, entity.id)
                assert stale is not None and fresh is not None
                attribute = next(iter(fields))
                setattr(fresh, attribute, "v2" if attribute == "name" else 2)
                await second.commit()
                setattr(stale, attribute, "v3" if attribute == "name" else 3)
                with pytest.raises(StaleDataError):
                    await first.commit()
        finally:
            await engine.dispose()
