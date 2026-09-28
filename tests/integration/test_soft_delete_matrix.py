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

from pyfly.context.request_context import RequestContext
from pyfly.data import transactional
from pyfly.data.auditing import AuditingHandler
from pyfly.data.pageable import KeysetPosition, Pageable, Sort
from pyfly.data.property_resolver import InvalidPropertyError
from pyfly.data.relational.sqlalchemy.entity import BaseEntity, SoftDeleteMixin, VersionedMixin
from pyfly.data.relational.sqlalchemy.soft_delete import SoftDeleteRepository
from pyfly.data.relational.sqlalchemy.specification import Specification
from pyfly.kernel.exceptions import OptimisticLockingFailureException
from pyfly.security.context import SecurityContext
from tests.integration._repository_harness import Datasources, dml, repository_datasources, sql_of
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
        with pytest.raises(OptimisticLockingFailureException):  # the stale holder can no longer edit the deleted row
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

        with pytest.raises(OptimisticLockingFailureException):
            await items.delete(stale)
        with pytest.raises(OptimisticLockingFailureException):
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
        with pytest.raises(OptimisticLockingFailureException):
            await items.save(stale)

        fresh = await items.find_by_id(two.id)
        other = await items.find_by_id(two.id)
        assert fresh is not None and other is not None
        fresh.name = "changed"
        await items.save(fresh)
        with pytest.raises(OptimisticLockingFailureException):
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


class _FixedClock:
    def get_now(self) -> datetime:
        return datetime(2031, 5, 4, 3, 2, 1, 123456, tzinfo=UTC)


class _AsyncSystemAuditor:
    async def get_current_auditor(self) -> str | None:
        return "system-job"


@pytest.mark.parametrize("how", ["by_id", "every_row", "entity"])
async def test_a_soft_delete_stamps_with_the_applications_auditing_handler(
    relational_backend: RelationalBackend, bob: None, how: str
) -> None:
    """The soft-delete ``UPDATE`` stamps as the auditing listener stamps an ORM update: with the registered
    handler's ``DateTimeProvider`` and ``AuditorAware`` (an ``async`` one too), not with the security context
    and the wall clock directly."""
    handler = AuditingHandler(_AsyncSystemAuditor(), _FixedClock())
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="audited"))
        handler.register()
        try:
            if how == "by_id":
                await items.delete_all_by_id([saved.id])
            elif how == "every_row":
                await items.delete_all()
            else:
                await items.delete(saved)
        finally:
            handler.unregister()
        async with datasources.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(ContractSoftItem.updated_by, ContractSoftItem.updated_at, ContractSoftItem.deleted_at)
                )
            ).one()
        expected = _FixedClock().get_now()
        assert row.updated_by == "system-job"
        assert row.updated_at == expected and row.deleted_at == expected


async def test_a_soft_delete_without_an_auditor_clears_updated_by(relational_backend: RelationalBackend) -> None:
    """As the auditing listener does for an ORM update: a job with no principal does not leave the last
    user's name on its change."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        saved = await items.save(ContractSoftItem(label="orphaned", updated_by="alice"))
        await items.delete_by_id(saved.id)
        async with datasources.engine.connect() as conn:
            updated_by = (await conn.execute(select(ContractSoftItem.updated_by))).scalar_one()
        assert updated_by is None


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
        assert [item.label for item in await items.find_all_including_deleted(label="gone")] == ["gone"]
        with pytest.raises(InvalidPropertyError):  # its filters are validated as the other reads' are
            await items.find_all_including_deleted(nope=1)
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


# ---------------------------------------------------------------------------------------------------------
# WP03-13: long id lists leave room for the UPDATE's own binds
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends("sqlite-file")
@pytest.mark.parametrize("count", [20_000, 40_000])
async def test_a_long_id_list_is_soft_deleted_within_the_variable_limit(
    relational_backend: RelationalBackend, bob: None, count: int
) -> None:
    """SQLite's limit counts every variable of a statement: an id chunk padded to the whole limit, plus the
    stamps the UPDATE sets, failed with 'too many SQL variables' from 16,385 ids."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = SoftItems()
        first, second, kept = await items.save_all([ContractSoftItem(label=f"i{n}") for n in range(3)])
        await items.delete_all_by_id([first.id, *(uuid.uuid4() for _ in range(count - 1))])
        await items.delete_all_by_id_in_batch([second.id, *(uuid.uuid4() for _ in range(count - 1))])
        deleted = await _deleted(datasources)
        assert deleted["i0"] is not None and deleted["i1"] is not None and deleted["i2"] is None
        assert len(await items.find_all_by_id([kept.id, *(uuid.uuid4() for _ in range(count - 1))])) == 1


# ---------------------------------------------------------------------------------------------------------
# WP03-07: the unit's copies follow what the UPDATE really changed
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends("sqlite-file", "pg")
async def test_only_the_rows_the_update_changed_are_stamped_in_the_unit(relational_backend: RelationalBackend) -> None:
    """Where ``UPDATE ... RETURNING`` exists, the unit's copy of a row the soft delete did not change (deleted
    behind the unit's back here) keeps its state: it used to get the stamps and a version bump the row does not
    have, and the next flush of that copy raised StaleDataError."""
    async with repository_datasources(relational_backend, *MODELS):
        items = VersionedItems()
        gone, live = await items.save_all([SdVersioned(name="gone"), SdVersioned(name="live")])

        @transactional
        async def delete_both() -> tuple[SdVersioned, SdVersioned]:
            held_gone, held_live = await items.find_by_id(gone.id), await items.find_by_id(live.id)
            assert held_gone is not None and held_live is not None
            behind = update(SdVersioned).where(SdVersioned.id == gone.id).values(deleted_at=func.now())
            await items._session.execute(behind.execution_options(synchronize_session=False))
            await items.delete_all_by_id([gone.id, live.id, uuid.uuid4()])
            held_gone.name = "renamed"
            await items._session.flush()  # the copy's version is still the row's
            return held_gone, held_live

        held_gone, held_live = await delete_both()
        assert held_gone.deleted_at is None and held_gone.version == gone.version + 1  # the rename's own bump
        assert held_live.deleted_at is not None and held_live.version == live.version + 1


@pytest.mark.backends("sqlite-file", "pg")
async def test_a_soft_delete_of_every_row_returns_only_the_keys_the_unit_holds(
    relational_backend: RelationalBackend,
) -> None:
    """``delete_all()`` returned the key of every row it changed, to find the unit's copies among them, so the
    memory it took grew with the table: the rows of the keys the unit holds are updated first, returning
    their keys, and every other active row by an ``UPDATE`` that returns nothing."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        items = VersionedItems()
        gone, live, *_rest = await items.save_all([SdVersioned(name=f"n{n}") for n in range(30)])

        @transactional
        async def delete_everything() -> tuple[SdVersioned, SdVersioned, list[str]]:
            held_gone, held_live = await items.find_by_id(gone.id), await items.find_by_id(live.id)
            assert held_gone is not None and held_live is not None
            behind = update(SdVersioned).where(SdVersioned.id == gone.id).values(deleted_at=func.now())
            await items._session.execute(behind.execution_options(synchronize_session=False))
            with datasources.counter() as counter:
                await items.delete_all()
            return held_gone, held_live, sql_of(counter, "UPDATE")

        held_gone, held_live, updates = await delete_everything()
        returning = [sql for sql in updates if "RETURNING" in sql]
        assert len(updates) == 2 and len(returning) == 1
        assert "ANY" in returning[0] or " IN (" in returning[0]  # only the held keys
        assert held_gone.deleted_at is None and held_gone.version == gone.version  # its row was not changed
        assert held_live.deleted_at is not None and held_live.version == live.version + 1
        assert await items.count() == 0
