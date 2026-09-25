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
"""The contract models are portable and behave the same on every relational lane (C161).

The repository contract suites run on these mappings (``tests/support/contract_models.py``), so this
proves the mappings themselves first, with plain SQLAlchemy sessions and no repository: the schema
creates on SQLite, PostgreSQL, MySQL and MariaDB; the relationship cascades and loads; the foreign key
is enforced, so a bulk delete that bypasses the ORM cascade fails everywhere instead of orphaning
children on one backend; optimistic locking detects a stale write; the composite key round-trips; the
soft-delete column stores an instant. The sqlite-file lane runs in the fast suite, the server lanes in
the integration suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import StaleDataError

from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import (
    CONTRACT_MODELS,
    ContractChild,
    ContractLine,
    ContractParent,
    ContractSoftItem,
    ContractVersioned,
)


async def _sessions(backend: RelationalBackend) -> async_sessionmaker[AsyncSession]:
    await backend.create_tables(*CONTRACT_MODELS)
    return async_sessionmaker(backend.create_engine(), expire_on_commit=False)


async def _count(factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _parent_by_id(parent_id: uuid.UUID) -> Select[tuple[ContractParent]]:
    return select(ContractParent).options(selectinload(ContractParent.children)).where(ContractParent.id == parent_id)


async def _parent_with_children(factory: async_sessionmaker[AsyncSession], *labels: str) -> uuid.UUID:
    async with factory() as session, session.begin():
        parent = ContractParent(name="parent")
        parent.children.extend(ContractChild(label=label, position=i) for i, label in enumerate(labels))
        session.add(parent)
    return parent.id


async def test_parent_children_round_trip_in_order(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    parent_id = await _parent_with_children(factory, "first", "second", "third")

    async with factory() as session:
        parent = (await session.execute(_parent_by_id(parent_id))).scalar_one()

    assert [child.label for child in parent.children] == ["first", "second", "third"]
    assert all(isinstance(child.id, int) for child in parent.children)  # database-generated identity
    assert {child.parent_id for child in parent.children} == {parent_id}
    assert parent.created_at is not None


async def test_orm_delete_cascades_to_children(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    parent_id = await _parent_with_children(factory, "a", "b")

    async with factory() as session, session.begin():
        await session.delete((await session.execute(_parent_by_id(parent_id))).scalar_one())

    assert await _count(factory, ContractParent) == 0
    assert await _count(factory, ContractChild) == 0


async def test_orphan_removal_deletes_the_detached_child(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    parent_id = await _parent_with_children(factory, "keep", "drop")

    async with factory() as session, session.begin():
        parent = (await session.execute(_parent_by_id(parent_id))).scalar_one()
        parent.children.remove(next(child for child in parent.children if child.label == "drop"))

    async with factory() as session:
        labels = (await session.execute(select(ContractChild.label))).scalars().all()
    assert labels == ["keep"]


async def test_bulk_delete_that_bypasses_the_cascade_is_rejected(relational_backend: RelationalBackend) -> None:
    """The foreign key has no ON DELETE action, so deleting parents in bulk while children exist
    fails on every lane. With SQLite's foreign keys off it silently orphaned the children."""
    factory = await _sessions(relational_backend)
    await _parent_with_children(factory, "a", "b")

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(delete(ContractParent))

    assert await _count(factory, ContractParent) == 1
    assert await _count(factory, ContractChild) == 2


async def test_versioned_entity_counts_flushes_and_rejects_a_stale_write(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        item = ContractVersioned(name="v1", quantity=1)
        session.add(item)
    assert item.version == 1

    async with factory() as session:
        stale = await session.get(ContractVersioned, item.id)
        await session.commit()  # end the read transaction; the copy stays in the identity map
        assert stale is not None

        async with factory() as other, other.begin():
            fresh = await other.get(ContractVersioned, item.id)
            assert fresh is not None
            fresh.quantity = 2
        assert fresh.version == 2

        stale.quantity = 3
        with pytest.raises(StaleDataError):
            await session.flush()
        await session.rollback()

    async with factory() as session:
        stored = await session.get(ContractVersioned, item.id)
    assert stored is not None
    assert (stored.version, stored.quantity) == (2, 2)


async def test_composite_key_round_trips_and_stays_unique(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        session.add_all([ContractLine(order_code="A-1", line_no=n, sku=f"sku-{n}", quantity=n) for n in (1, 2)])
        session.add(ContractLine(order_code="B-1", line_no=1, sku="other"))

    async with factory() as session:
        line = await session.get(ContractLine, ("A-1", 2))
    assert line is not None
    assert (line.sku, line.quantity) == ("sku-2", 2)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(ContractLine(order_code="A-1", line_no=1, sku="duplicate"))


async def test_soft_delete_column_stores_the_deletion_instant(relational_backend: RelationalBackend) -> None:
    factory = await _sessions(relational_backend)
    async with factory() as session, session.begin():
        item = ContractSoftItem(label="soft")
        session.add(item)
    assert not item.is_deleted

    deleted_at = datetime(2026, 9, 25, 12, 30, 15, tzinfo=UTC)
    async with factory() as session, session.begin():
        stored = await session.get(ContractSoftItem, item.id)
        assert stored is not None
        stored.deleted_at = deleted_at

    async with factory() as session:
        reloaded = await session.get(ContractSoftItem, item.id)
    assert reloaded is not None
    assert reloaded.is_deleted
    assert reloaded.deleted_at is not None
    # A UTC instant in whole seconds reads back as the same wall-clock time on every lane, whether the
    # backend returns it aware (PostgreSQL) or naive (SQLite, MySQL, MariaDB).
    assert reloaded.deleted_at.replace(tzinfo=None) == deleted_at.replace(tzinfo=None)
