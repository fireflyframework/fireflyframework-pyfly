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
"""``SoftDeleteRepository`` on every lane (WP03-06, WP03-07; the entity-type resolution of WP03-04 is in
``tests/data/test_soft_delete.py``).

- A soft delete is an ``UPDATE ... WHERE pk = :id AND deleted_at IS NULL`` by primary key, so it works for
  the detached entities every call outside a transaction returns, and for entities of another session (they
  used to be stamped in memory and silently never written, C056). It is one statement, with no SELECT.
- Every soft delete, bulk ones included, bumps the version (a stale holder can no longer edit a deleted row),
  stamps ``updated_at``/``updated_by``, leaves an already deleted row's ``deleted_at`` alone, and checks the
  version an entity carries (C142).
- Reads exclude deleted rows on every path; the unit's own copies are kept in step, so a deleted entity the
  unit holds is not found again.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import String, func, select, update
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.context.request_context import RequestContext
from pyfly.data import transactional
from pyfly.data.pageable import KeysetPosition, Pageable, Sort
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin, VersionedMixin
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.security.context import SecurityContext
from tests.integration._repository_harness import Datasources, dml, repository_datasources
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import ContractSoftItem


class SdVersioned(SoftDeleteMixin, VersionedMixin, BaseEntity):
    """A soft-deletable, optimistically locked entity."""

    __tablename__ = "sd_versioned"

    name: Mapped[str] = mapped_column(String(40))


class SdTokenVersioned(SoftDeleteMixin, BaseEntity):
    """Optimistically locked by a random token: a version SQL cannot compute (``version + 1`` does not apply)."""

    __tablename__ = "sd_token_versioned"

    name: Mapped[str] = mapped_column(String(40))
    token: Mapped[str] = mapped_column(String(32), nullable=False)
    __mapper_args__ = {"version_id_col": token, "version_id_generator": lambda _version: uuid.uuid4().hex}


class SoftItems(SoftDeleteRepository[ContractSoftItem, uuid.UUID]):
    """The documented form (C052)."""


class VersionedItems(SoftDeleteRepository[SdVersioned, uuid.UUID]):
    pass


class TokenItems(SoftDeleteRepository[SdTokenVersioned, uuid.UUID]):
    pass


MODELS = (ContractSoftItem, SdVersioned, SdTokenVersioned)


async def _deleted(datasources: Datasources) -> dict[str, datetime | None]:
    async with datasources.engine.connect() as conn:
        rows = await conn.execute(select(ContractSoftItem.label, ContractSoftItem.deleted_at))
        return {str(label): deleted_at for label, deleted_at in rows}


@pytest.fixture
def bob() -> Iterator[None]:
    context = RequestContext.init()
    context.security_context = SecurityContext(user_id="bob")
    try:
        yield
    finally:
        RequestContext.clear()


# ---------------------------------------------------------------------------------------------------------
# WP03-06: soft delete by key, whatever session the entity came from
# ---------------------------------------------------------------------------------------------------------


async def test_a_detached_entity_is_soft_deleted_with_one_update(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        kept, first, second = await items.save_all([ContractSoftItem(label=name) for name in ("kept", "a", "b")])
        detached = await items.find_by_id(first.id)
        assert detached is not None

        with datasources.counter() as counter:
            await items.delete(detached)
        assert dml(counter) == {"UPDATE": 1}
        assert detached.deleted_at is not None  # the caller's copy reflects the delete
        await items.delete_all([second])
        deleted = await _deleted(datasources)
        assert deleted["kept"] is None and deleted["a"] is not None and deleted["b"] is not None
        assert await items.find_by_id(first.id) is None
        assert await items.count() == 1


async def test_an_entity_of_another_session_is_soft_deleted(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="other"))
        async with datasources.registry.session_factory()() as other:
            held = await other.get(ContractSoftItem, saved.id)
            assert held is not None
            await items.delete(held)
            await other.rollback()
        assert (await _deleted(datasources))["other"] is not None


async def test_delete_by_id_is_one_update(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="x"))
        with datasources.counter() as counter:
            await items.delete_by_id(saved.id)
            await items.delete_by_id(uuid.uuid4())  # missing: ignored
        assert dml(counter) == {"UPDATE": 2}
        assert await items.exists_by_id(saved.id) is False


async def test_a_new_entity_is_ignored(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        with datasources.counter() as counter:
            await items.delete(ContractSoftItem(label="never saved"))
        assert dml(counter) == {}

        @transactional
        async def add_then_delete() -> None:
            pending = ContractSoftItem(label="pending")
            items._session.add(pending)  # pending in the unit, never flushed
            await items.delete(pending)

        await add_then_delete()
        assert await _deleted(datasources) == {}  # deleting a pending entity means not inserting it


# ---------------------------------------------------------------------------------------------------------
# WP03-07: versions, audit stamps, already deleted rows
# ---------------------------------------------------------------------------------------------------------


async def test_every_soft_delete_bumps_the_version(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = VersionedItems()
        one, two, three = await items.save_all([SdVersioned(name=name) for name in ("one", "two", "three")])
        stale = await items.find_all_including_deleted()
        await items.delete_all_by_id([one.id])
        await items.delete_by_id(two.id)
        await items.delete_all()
        async with datasources.engine.connect() as conn:
            versions = dict((await conn.execute(select(SdVersioned.name, SdVersioned.version))).all())
        assert versions == {"one": 2, "two": 2, "three": 2}

        holder = next(item for item in stale if item.name == "one")
        holder.name = "edited after delete"
        with pytest.raises(StaleDataError):  # the stale holder can no longer edit the deleted row
            await items.save(holder)


async def test_a_stale_entity_is_not_soft_deleted(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = VersionedItems()
        saved = await items.save(SdVersioned(name="v"))
        stale = await items.find_by_id(saved.id)
        fresh = await items.find_by_id(saved.id)
        assert stale is not None and fresh is not None
        fresh.name = "changed"
        await items.save(fresh)

        with pytest.raises(StaleDataError):
            await items.delete(stale)
        with pytest.raises(StaleDataError):
            await items.delete_all([stale])
        assert await items.count() == 1
        await items.delete(fresh)
        assert await items.count() == 0
        assert fresh.version == 3  # in step with the row: the save made it 2, the delete 3
        assert datasources.checked_out() == 0


async def test_a_version_sql_cannot_compute_goes_through_the_orm(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = TokenItems()
        one, two = await items.save_all([SdTokenVersioned(name="one"), SdTokenVersioned(name="two")])
        stale = await items.find_by_id(one.id)
        assert stale is not None
        await items.delete_all_by_id([one.id])
        stale.name = "edited after delete"
        with pytest.raises(StaleDataError):
            await items.save(stale)

        fresh = await items.find_by_id(two.id)
        other = await items.find_by_id(two.id)
        assert fresh is not None and other is not None
        fresh.name = "changed"
        await items.save(fresh)
        with pytest.raises(StaleDataError):
            await items.delete(other)
        before = fresh.token
        await items.delete(fresh)
        assert fresh.deleted_at is not None and fresh.token != before
        async with datasources.engine.connect() as conn:
            tokens = dict((await conn.execute(select(SdTokenVersioned.name, SdTokenVersioned.token))).all())
        assert tokens["changed"] == fresh.token
        assert await items.count() == 0


async def test_a_bulk_soft_delete_stamps_the_audit_columns(relational_backend: RelationalBackend, bob: None) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save_all([ContractSoftItem(label=name) for name in ("a", "b")])
        async with datasources.engine.begin() as conn:
            await conn.execute(update(ContractSoftItem).values(updated_at=datetime(2000, 1, 1, tzinfo=UTC)))
        await items.delete_all_by_id([item.id for item in saved])
        async with datasources.engine.connect() as conn:
            rows = (await conn.execute(select(ContractSoftItem.updated_by, ContractSoftItem.updated_at))).all()
        assert [updated_by for updated_by, _at in rows] == ["bob", "bob"]
        assert all(updated_at.year >= 2026 for _by, updated_at in rows)


async def test_an_already_deleted_row_keeps_its_deleted_at(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="once"))
        await items.delete_by_id(saved.id)
        first = (await _deleted(datasources))["once"]
        await items.delete_by_id(saved.id)
        await items.delete_all_by_id([saved.id])
        await items.delete_all()
        await items.delete_all_in_batch()
        assert (await _deleted(datasources))["once"] == first


# ---------------------------------------------------------------------------------------------------------
# Reads, restore and hard delete
# ---------------------------------------------------------------------------------------------------------


async def test_every_read_excludes_deleted_rows(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS):
        items = SoftItems()
        alive, gone = await items.save_all([ContractSoftItem(label="alive"), ContractSoftItem(label="gone")])
        await items.delete_by_id(gone.id)
        every = Specification[ContractSoftItem](lambda root, q: q.where(root.label != ""))

        assert [item.label for item in await items.find_all()] == ["alive"]
        assert await items.count() == 1
        assert await items.exists_by_id(gone.id) is False and await items.exists_by_id(alive.id) is True
        assert await items.find_by_id(gone.id) is None
        assert [item.label for item in await items.find_all_by_id([alive.id, gone.id])] == ["alive"]
        assert (await items.find_all(Pageable.of(1, 10))).total == 1
        assert len((await items.find_slice(Pageable.of(1, 10))).items) == 1
        assert len((await items.scroll(Sort.by("label"))).items) == 1
        assert len([item async for item in items.stream_all()]) == 1
        assert len(await items.find_all_by_spec(every)) == 1
        assert (await items.find_all_by_spec_paged(every, Pageable.of(1, 5))).total == 1
        assert sorted(item.label for item in await items.find_all_including_deleted()) == ["alive", "gone"]
        window = await items.scroll(Sort.by("label"), KeysetPosition.of(label="a", id=uuid.UUID(int=0)))
        assert [item.label for item in window.items] == ["alive"]


async def test_the_units_own_copy_follows_a_soft_delete(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS):
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="held"))

        @transactional
        async def delete_what_the_unit_holds() -> tuple[bool, bool, bool]:
            held = await items.find_by_id(saved.id)
            assert held is not None
            await items.delete_by_id(saved.id)
            return (
                held.deleted_at is not None,
                await items.find_by_id(saved.id) is None,
                await items.exists_by_id(saved.id),
            )

        assert await delete_what_the_unit_holds() == (True, True, False)


async def test_restore_and_hard_delete(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = VersionedItems()
        saved = await items.save(SdVersioned(name="back"))
        await items.delete_by_id(saved.id)
        restored = await items.restore(saved.id)
        assert restored is not None and restored.deleted_at is None and restored.version == 3
        assert await items.restore(uuid.uuid4()) is None
        assert await items.count() == 1

        await items.delete_by_id(saved.id)
        await items.hard_delete(saved.id)
        async with datasources.engine.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(SdVersioned))).scalar_one() == 0
