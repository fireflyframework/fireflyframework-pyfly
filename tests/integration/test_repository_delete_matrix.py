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
"""Deletes, existence checks, composite keys and long id lists, on every lane.

- The Spring delete family deletes entity by entity through the ORM, so ``delete_all_by_id`` and
  ``delete_all()`` cascade to children, check versions and fire delete listeners instead of failing on the
  foreign key or orphaning children depending on the backend (C053, C133); a mapper with none of those
  deletes in bulk. ``delete_all_in_batch`` and ``delete_all_by_id_in_batch`` are the explicit bulk forms.
- ``exists_by_id`` sends ``SELECT 1 ... LIMIT 1`` instead of loading the row, and answers from the unit's
  identity map without a statement (C129, C130).
- Composite keys work in ``find_by_id``, ``find_all_by_id`` and the deletes, matching the whole key, and a
  scalar id for a composite key is refused (C134).
- Id lists longer than the dialect's parameter limit are chunked inside the unit (C139), and on PostgreSQL an
  id list is one ``= ANY`` statement text whatever its length, so asyncpg's statement cache keeps it (C172).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Integer, String, event, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.exc import StaleDataError

from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from tests.integration._repository_harness import Datasources, dml, repository_datasources, sql_of
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import (
    CONTRACT_MODELS,
    ContractChild,
    ContractLine,
    ContractParent,
    ContractVersioned,
)


class RdAudited(Base):
    """A plain entity whose deletes a listener records."""

    __tablename__ = "rd_audited"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(40))


DELETED: list[int] = []


@event.listens_for(RdAudited, "after_delete")
def _record_delete(_mapper: Any, _connection: Any, target: RdAudited) -> None:
    DELETED.append(target.id)


class ParentRepository(Repository[ContractParent, uuid.UUID]):
    pass


class LineRepository(Repository[ContractLine, tuple[str, int]]):
    pass


class VersionedRepository(Repository[ContractVersioned, uuid.UUID]):
    pass


class AuditedRepository(Repository[RdAudited, int]):
    pass


class ChildRepository(Repository[ContractChild, int]):
    pass


MODELS = (*CONTRACT_MODELS, RdAudited)


async def _count(datasources: Datasources, model: type) -> int:
    async with datasources.engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(model))).scalar_one())


async def _family(parents: ParentRepository, count: int, children: int = 2) -> list[ContractParent]:
    families = []
    for n in range(count):
        parent = ContractParent(name=f"p{n}")
        parent.children.extend(ContractChild(label=f"c{n}.{k}", position=k) for k in range(children))
        families.append(parent)
    return await parents.save_all(families)


async def _lines(datasources: Datasources, *keys: tuple[str, int]) -> None:
    async with datasources.engine.begin() as conn:
        await conn.execute(insert(ContractLine), [{"order_code": c, "line_no": n, "sku": f"{c}{n}"} for c, n in keys])


# ---------------------------------------------------------------------------------------------------------
# WP03-03: deletes cascade unless the caller asks for a batch
# ---------------------------------------------------------------------------------------------------------


async def test_delete_all_by_id_cascades_to_the_children(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        first, second, third = await _family(parents, 3)
        await parents.delete_all_by_id([first.id, second.id])
        assert await _count(datasources, ContractParent) == 1
        assert await _count(datasources, ContractChild) == 2  # third's children, none orphaned
        assert await parents.find_by_id(third.id) is not None


async def test_delete_all_cascades_to_every_child(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        await _family(parents, 3)
        await parents.delete_all()
        assert await _count(datasources, ContractParent) == 0
        assert await _count(datasources, ContractChild) == 0


async def test_delete_of_detached_entities_cascades(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        first, second, third = await _family(parents, 3)
        await parents.delete(first)  # detached: every entity a call returns outside a transaction is
        await parents.delete_all([second])
        assert await _count(datasources, ContractParent) == 1
        assert await _count(datasources, ContractChild) == 2
        await parents.delete(ContractParent(name="never saved"))  # a new entity: nothing to delete
        await parents.delete_by_id(uuid.uuid4())  # a missing id: ignored, as in Spring
        await parents.delete(first)  # its row is gone already: ignored
        assert await _count(datasources, ContractParent) == 1
        assert third.id is not None


async def test_the_batch_deletes_are_bulk_and_bypass_the_cascade(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        families = await _family(parents, 2)
        with pytest.raises(IntegrityError):  # the foreign key refuses what the ORM cascade would have done
            await parents.delete_all_by_id_in_batch([families[0].id])
        with pytest.raises(IntegrityError):
            await parents.delete_all_in_batch()
        assert await _count(datasources, ContractParent) == 2

        bare = await parents.save_all([ContractParent(name="lone1"), ContractParent(name="lone2")])
        with datasources.counter() as counter:
            await parents.delete_all_in_batch(bare)
        assert dml(counter) == {"DELETE": 1}
        assert await _count(datasources, ContractParent) == 2


async def test_a_mapper_without_cascades_deletes_in_bulk(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _lines(datasources, ("A", 1), ("A", 2), ("B", 1), ("C", 1))
        lines = LineRepository()
        with datasources.counter() as counter:
            await lines.delete_all_by_id([("A", 2), ("C", 1)])
        assert dml(counter) == {"DELETE": 1}
        with datasources.counter() as counter:
            await lines.delete_all_by_id_in_batch([("B", 1)])
        assert dml(counter) == {"DELETE": 1}
        assert [(line.order_code, line.line_no) for line in await lines.find_all()] == [("A", 1)]
        await lines.delete_all()
        assert await _count(datasources, ContractLine) == 0


async def test_delete_listeners_run_for_every_entity(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS):
        audited = AuditedRepository()
        await audited.save_all([RdAudited(id=n, name=f"a{n}") for n in (1, 2, 3, 4)])
        DELETED.clear()
        await audited.delete_all_by_id([1, 2])
        await audited.delete_all()
        assert sorted(DELETED) == [1, 2, 3, 4]


async def test_a_stale_version_is_rejected_by_every_delete(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        versioned = VersionedRepository()
        saved = await versioned.save(ContractVersioned(name="v", quantity=1))
        stale = await versioned.find_by_id(saved.id)
        fresh = await versioned.find_by_id(saved.id)
        assert stale is not None and fresh is not None
        fresh.quantity = 2
        await versioned.save(fresh)

        with pytest.raises(StaleDataError):
            await versioned.delete(stale)
        with pytest.raises(StaleDataError):
            await versioned.delete_all([stale])
        assert await _count(datasources, ContractVersioned) == 1
        await versioned.delete_all_by_id([saved.id])  # by id: no version to compare
        assert await _count(datasources, ContractVersioned) == 0


async def test_deleting_inside_a_transaction_sees_the_units_own_entities(
    relational_backend: RelationalBackend,
) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()

        @transactional
        async def create_and_remove() -> bool:
            kept, dropped = await _family(parents, 2)
            await parents.delete_all_by_id([dropped.id])
            return await parents.exists_by_id(dropped.id) or not await parents.exists_by_id(kept.id)

        assert await create_and_remove() is False
        assert await _count(datasources, ContractParent) == 1
        assert await _count(datasources, ContractChild) == 2


# ---------------------------------------------------------------------------------------------------------
# WP03-08: exists is a LIMIT 1 probe, or no statement at all
# ---------------------------------------------------------------------------------------------------------


async def test_exists_by_id_is_a_limit_one_probe(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        (parent,) = await _family(parents, 1)
        with datasources.counter() as counter:
            assert await parents.exists_by_id(parent.id) is True
            assert await parents.exists_by_id(uuid.uuid4()) is False
        (probe, _missing) = sql_of(counter, "SELECT")
        assert "LIMIT" in probe.upper()
        assert "contract_parent.name" not in probe and "count(" not in probe.lower()


async def test_exists_by_id_answers_from_the_units_identity_map(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        parents = ParentRepository()
        (parent,) = await _family(parents, 1)

        @transactional
        async def load_then_ask() -> tuple[bool, dict[str, int]]:
            loaded = await parents.find_by_id(parent.id)  # the unit holds it while it is referenced
            with datasources.counter() as counter:
                answer = await parents.exists_by_id(parent.id)
            assert loaded is not None
            return answer, dml(counter)

        answer, counts = await load_then_ask()
        assert answer is True and counts == {}


# ---------------------------------------------------------------------------------------------------------
# WP03-09: composite keys
# ---------------------------------------------------------------------------------------------------------


async def test_composite_keys_match_the_whole_key(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _lines(datasources, ("A", 1), ("A", 2), ("B", 1), ("B", 2))
        lines = LineRepository()
        found = await lines.find_all_by_id([("A", 2), ("B", 1), ("Z", 9)])
        assert sorted((line.order_code, line.line_no) for line in found) == [("A", 2), ("B", 1)]
        line = await lines.find_by_id(("B", 2))
        assert line is not None and line.sku == "B2"
        assert await lines.exists_by_id({"order_code": "A", "line_no": 1}) is True
        assert await lines.exists_by_id(("A", 3)) is False

        await lines.delete_all_by_id([("A", 1), ("B", 2)])
        remaining = sorted((line.order_code, line.line_no) for line in await lines.find_all())
        assert remaining == [("A", 2), ("B", 1)]


async def test_a_scalar_id_for_a_composite_key_is_refused(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _lines(datasources, ("A", 1), ("B", 1))
        lines = LineRepository()
        with pytest.raises(TypeError, match="composite primary key"):
            await lines.find_all_by_id(["A"])  # type: ignore[list-item]
        with pytest.raises(TypeError, match="composite primary key"):
            await lines.delete_all_by_id(["A"])  # type: ignore[list-item]
        assert await _count(datasources, ContractLine) == 2


# ---------------------------------------------------------------------------------------------------------
# WP03-13 / WP03-14: long id lists and statement texts
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends("sqlite-file", "pg")
async def test_id_lists_past_the_parameter_limit_are_chunked(relational_backend: RelationalBackend) -> None:
    """40,000 ids pass SQLite's 32,766 variables and asyncpg's 32,767 arguments."""
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        await _lines(datasources, *[("L", n) for n in range(1, 101)])
        children = ChildRepository()
        parents = ParentRepository()
        (parent,) = await _family(parents, 1, children=100)
        wanted = [child.id for child in parent.children] + list(range(10_000_000, 10_040_000))
        found = await children.find_all_by_id(wanted)
        assert len(found) == 100
        lines = LineRepository()
        keys = [("L", n) for n in range(1, 50)] + [("X", n) for n in range(20_000)]
        assert len(await lines.find_all_by_id(keys)) == 49
        await lines.delete_all_by_id(keys)
        assert await _count(datasources, ContractLine) == 51


async def test_id_lists_share_a_few_statement_texts(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *MODELS) as datasources:
        children = ChildRepository()
        with datasources.counter() as counter:
            for length in range(1, 65):
                await children.find_all_by_id(list(range(1, length + 1)))
        texts = set(sql_of(counter, "SELECT"))
        assert len(texts) == (1 if datasources.dialect == "postgresql" else 7)
