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
"""``save``/``save_all`` persist or merge, at the minimum statement count, on every lane (WP03-01, WP03-02).

- A new entity is one ``INSERT``: no ``refresh()`` SELECT after it (F11); server-generated values come back
  through ``RETURNING`` where the backend has it, and through one targeted SELECT of just those columns where
  it does not (MySQL). ``save_all(n)`` sends no per-entity SELECT.
- Spring's ``save``: an entity that is not new is merged, not inserted, so an entity built from a DTO with an
  existing id updates its row instead of raising ``IntegrityError`` (C054, C132), an entity attached to
  another session is accepted, and a detached entity (every entity a repository call returned outside a
  transaction) is re-attached and updated with one ``UPDATE``. "New" is Spring's rule: a ``Persistable``
  ``is_new()`` hook, else a ``None`` version, else a ``None`` primary key.
- Optimistic locking holds across the request boundary: a DTO or a detached copy carrying a stale version
  raises ``StaleDataError``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Integer, String, select, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import Propagation
from tests.integration._repository_harness import Datasources, dml, repository_datasources
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import CONTRACT_MODELS, ContractChild, ContractParent, ContractVersioned


class RsStamped(Base):
    """A row whose ``code`` the database fills in (a server default)."""

    __tablename__ = "rs_stamped"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(40))
    code: Mapped[str] = mapped_column(String(20), server_default=text("'srv'"))


class RsTicket(Base):
    """An entity with an application-assigned key that tells the repository itself when it is new."""

    __tablename__ = "rs_ticket"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(40))

    def is_new(self) -> bool:
        return self.title.startswith("new:")


class ParentRepository(Repository[ContractParent, uuid.UUID]):
    pass


class ChildRepository(Repository[ContractChild, int]):
    pass


class VersionedRepository(Repository[ContractVersioned, uuid.UUID]):
    pass


class StampedRepository(Repository[RsStamped, int]):
    pass


class TicketRepository(Repository[RsTicket, str]):
    pass


MODELS = (*CONTRACT_MODELS, RsStamped, RsTicket)


async def _names(datasources: Datasources, model: type) -> list[str]:
    async with datasources.engine.connect() as conn:
        return sorted((await conn.execute(select(model.name))).scalars().all())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------------------------------------
# WP03-01: no refresh after the flush
# ---------------------------------------------------------------------------------------------------------


async def test_save_of_a_new_entity_is_one_insert(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        with datasources.counter() as counter:
            saved = await parents.save(ContractParent(name="one"))
        assert dml(counter) == {"INSERT": 1}
        assert isinstance(saved.id, uuid.UUID) and saved.created_at is not None  # readable after the unit
        assert counter.commits == 1


async def test_save_all_sends_no_select_per_entity(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parent = await ParentRepository().save(ContractParent(name="p"))
        children = ChildRepository()
        with datasources.counter() as counter:
            saved = await children.save_all([ContractChild(parent_id=parent.id, label=f"c{n}") for n in range(20)])
        counts = dml(counter)
        assert "SELECT" not in counts
        assert 1 <= counts["INSERT"] <= 20  # batched where the driver can (RETURNING), row by row elsewhere
        assert all(isinstance(child.id, int) for child in saved)  # database-generated keys came back
        assert len({child.id for child in saved}) == 20


async def test_server_generated_values_come_back_with_the_insert(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        stamped = StampedRepository()
        with datasources.counter() as counter:
            saved = await stamped.save(RsStamped(name="s"))
            both = await stamped.save_all([RsStamped(name="a"), RsStamped(name="b")])
        assert saved.code == "srv" and [row.code for row in both] == ["srv", "srv"]
        counts = dml(counter)
        if datasources.dialect == "mysql":
            # No RETURNING: one targeted SELECT of the generated columns per call, never a refresh per entity.
            assert counts.get("SELECT") == 2
        else:
            assert "SELECT" not in counts


# ---------------------------------------------------------------------------------------------------------
# WP03-02: persist or merge
# ---------------------------------------------------------------------------------------------------------


async def test_an_entity_built_from_a_dto_with_an_existing_id_updates_its_row(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        existing = await parents.save(ContractParent(name="original"))

        with datasources.counter() as counter:
            merged = await parents.save(ContractParent(id=existing.id, name="from-dto", active=True))
        assert dml(counter) == {"SELECT": 1, "UPDATE": 1}
        assert merged.id == existing.id and merged.name == "from-dto"
        assert await _names(datasources, ContractParent) == ["from-dto"]


async def test_an_assigned_id_that_does_not_exist_yet_is_inserted(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        new_id = uuid.uuid4()
        saved = await ParentRepository().save(ContractParent(id=new_id, name="assigned"))
        assert saved.id == new_id
        assert await _names(datasources, ContractParent) == ["assigned"]


async def test_a_detached_entity_is_reattached_and_updated_with_one_statement(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        saved = await parents.save(ContractParent(name="before"))
        loaded = await parents.find_by_id(saved.id)  # detached once its read unit ends
        assert loaded is not None
        loaded.name = "after"

        with datasources.counter() as counter:
            again = await parents.save(loaded)
        assert dml(counter) == {"UPDATE": 1}
        assert again is loaded
        assert await _names(datasources, ContractParent) == ["after"]


async def test_an_entity_attached_to_another_session_is_merged(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        saved = await parents.save(ContractParent(name="outer"))

        async with datasources.registry.session_factory()() as other:
            parent = await other.get(ContractParent, saved.id)  # attached to a session that stays open
            assert parent is not None
            parent.name = "renamed"
            renamed = await parents.save(parent)
            assert renamed is not parent  # the repository's unit holds its own managed copy
            await other.rollback()
        assert renamed.name == "renamed"
        assert await _names(datasources, ContractParent) == ["renamed"]


@pytest.mark.backends("pg", "mysql", "mariadb")
async def test_an_entity_loaded_in_an_outer_unit_is_merged_into_a_requires_new_unit(
    relational_backend: RelationalBackend,
) -> None:
    """The outer unit keeps its connection while the inner one writes, which SQLite's one writer refuses."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        saved = await parents.save(ContractParent(name="outer"))

        @transactional(propagation=Propagation.REQUIRES_NEW)
        async def rename_in_its_own_unit(parent: ContractParent) -> ContractParent:
            parent.name = "renamed"
            return await parents.save(parent)

        @transactional
        async def load_then_save_elsewhere() -> ContractParent:
            parent = await parents.find_by_id(saved.id)
            assert parent is not None
            return await rename_in_its_own_unit(parent)

        renamed = await load_then_save_elsewhere()
        assert renamed.name == "renamed"
        assert await _names(datasources, ContractParent) == ["renamed"]


async def test_the_persistable_hook_decides_whether_an_entity_is_new(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        tickets = TicketRepository()
        with datasources.counter() as counter:
            await tickets.save(RsTicket(id="T-1", title="new: first"))
        assert dml(counter) == {"INSERT": 1}  # new by its own word: no merge SELECT
        with datasources.counter() as counter:
            updated = await tickets.save(RsTicket(id="T-1", title="edited"))
        assert dml(counter) == {"SELECT": 1, "UPDATE": 1}
        assert updated.title == "edited"


async def test_save_all_merges_existing_dtos_with_one_select(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        first, second = await parents.save_all([ContractParent(name="a"), ContractParent(name="b")])
        with datasources.counter() as counter:
            saved = await parents.save_all(
                [
                    ContractParent(id=first.id, name="a2", active=True),
                    ContractParent(id=second.id, name="b2", active=True),
                    ContractParent(id=uuid.uuid4(), name="c", active=True),
                    ContractParent(name="d"),
                ]
            )
        counts = dml(counter)
        assert counts["SELECT"] == 1  # one prefetch of the existing keys, never one per entity
        assert counts["UPDATE"] in (1, 2) and counts["INSERT"] in (1, 2)
        assert [parent.name for parent in saved] == ["a2", "b2", "c", "d"]
        assert await _names(datasources, ContractParent) == ["a2", "b2", "c", "d"]


# ---------------------------------------------------------------------------------------------------------
# Optimistic locking across the request boundary
# ---------------------------------------------------------------------------------------------------------


async def test_a_new_versioned_entity_is_persisted_without_a_select(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        with datasources.counter() as counter:
            saved = await VersionedRepository().save(ContractVersioned(name="v", quantity=1))
        assert dml(counter) == {"INSERT": 1}
        assert saved.version == 1


async def test_a_versioned_dto_with_the_current_version_updates(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        versioned = VersionedRepository()
        saved = await versioned.save(ContractVersioned(name="v", quantity=1))
        dto = ContractVersioned(id=saved.id, version=saved.version, name="v", quantity=5)
        merged = await versioned.save(dto)
        assert (merged.quantity, merged.version) == (5, 2)
        async with datasources.engine.connect() as conn:
            assert (await conn.execute(select(ContractVersioned.quantity))).scalar_one() == 5


async def test_a_stale_version_is_rejected_from_a_dto_and_from_a_detached_copy(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        versioned = VersionedRepository()
        saved = await versioned.save(ContractVersioned(name="v", quantity=1))
        stale_copy = await versioned.find_by_id(saved.id)
        assert stale_copy is not None
        fresh = await versioned.find_by_id(saved.id)
        assert fresh is not None
        fresh.quantity = 2
        await versioned.save(fresh)  # version 2 now

        with pytest.raises(StaleDataError):
            await versioned.save(ContractVersioned(id=saved.id, version=1, name="v", quantity=9))
        stale_copy.quantity = 3
        with pytest.raises(StaleDataError):
            await versioned.save(stale_copy)
        async with datasources.engine.connect() as conn:
            row = (await conn.execute(select(ContractVersioned.quantity, ContractVersioned.version))).one()
        assert tuple(row) == (2, 2)
        assert datasources.checked_out() == 0
