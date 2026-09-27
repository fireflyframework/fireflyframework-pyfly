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
"""Where a repository call's auto unit ends and a ``@transactional`` boundary begins (the WP01 follow-ups).

- A write auto unit is the transaction of the repository call that opened it (Spring Data's repository
  methods are ``@Transactional``): a ``@transactional`` call made inside a repository write method joins it
  (``REQUIRED``, ``SUPPORTS``, ``MANDATORY``), takes a savepoint on it (``NESTED``), or is refused
  (``NEVER``). Before, it opened a second transaction that committed on its own on PostgreSQL, MySQL and
  MariaDB, whatever the repository method did afterwards, and failed fast on SQLite's one writer.
- A read auto unit is not a transaction (on PostgreSQL it runs on an ``AUTOCOMMIT`` connection): a boundary
  inside a read method sees none, and a ``REQUIRED`` write there commits in a unit of its own.
- A method named like a read that writes (``get_or_create``, ``find_or_create_by_email``) runs in a write
  unit; a name that merely mentions a write word after its criteria (``find_by_update_time``) stays a read.
- A repository method decorated with ``@transactional`` runs in the unit its own boundary begins.
- Entities returned from auto units stay readable even when the application's session factory expires on
  commit: an auto unit never expires what its call returns.
"""

from __future__ import annotations

import contextlib
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from pyfly.data import transactional
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transaction_manager import bind_primary_session_factory
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Propagation,
    current_unit_of_work,
    is_transaction_active,
)
from tests.integration._repository_harness import Datasources, repository_datasources
from tests.support.backend_matrix import RelationalBackend
from tests.support.contract_models import CONTRACT_MODELS, ContractParent


class LedgerFailure(Exception):
    pass


class Ledger:
    """A service the repository calls into."""

    def __init__(self, parents: Repository[ContractParent, uuid.UUID]) -> None:
        self.parents = parents

    @transactional
    async def record(self, name: str) -> None:
        await self.parents.save(ContractParent(name=name))

    @transactional(propagation=Propagation.MANDATORY)
    async def record_mandatory(self, name: str) -> None:
        await self.parents.save(ContractParent(name=name))

    @transactional(propagation=Propagation.NEVER)
    async def outside_only(self) -> None:
        return None

    @transactional(propagation=Propagation.NESTED)
    async def try_record(self, name: str) -> None:
        await self.parents.save(ContractParent(name=name))
        raise LedgerFailure("the nested step fails after writing")


class ParentRepository(Repository[ContractParent, uuid.UUID]):
    ledger: Ledger

    async def register_family(self, name: str, *, fail: bool = False) -> None:
        """A write method: its auto unit is the transaction the ledger joins."""
        await self.save(ContractParent(name=name))
        await self.ledger.record(f"{name}-ledger")
        if fail:
            raise LedgerFailure("the repository method fails after the ledger wrote")

    async def register_mandatory(self, name: str) -> bool:
        await self.ledger.record_mandatory(name)
        return is_transaction_active()

    async def register_never(self) -> None:
        await self.ledger.outside_only()

    async def register_with_a_nested_try(self, name: str) -> None:
        await self.save(ContractParent(name=name))
        with contextlib.suppress(LedgerFailure):
            await self.ledger.try_record(f"{name}-nested")

    async def find_then_record(self, name: str) -> bool:
        """A read method: it has no transaction, so the ledger's REQUIRED boundary begins its own unit."""
        active = is_transaction_active()
        await self.ledger.record(name)
        return active

    async def get_or_create(self, name: str) -> ContractParent:
        """A read-prefixed name that writes: it runs in a write unit."""
        found = await self.find_all(name=name)
        return found[0] if found else await self.save(ContractParent(name=name))

    async def find_or_create_by_name(self, name: str) -> ContractParent:
        found = await self.find_all(name=name)
        return found[0] if found else await self.save(ContractParent(name=name))

    @transactional(name="seeding")
    async def find_or_seed(self, name: str) -> tuple[str | None, bool]:
        """Decorated: its own boundary begins the unit it runs in (not a read auto unit)."""
        unit = current_unit_of_work(self.datasource)
        assert unit is not None
        await self.save(ContractParent(name=name))
        return unit.definition.name, unit.auto


def _repositories() -> ParentRepository:
    parents = ParentRepository()
    parents.ledger = Ledger(parents)
    return parents


async def _names(datasources: Datasources) -> list[str]:
    async with datasources.engine.connect() as conn:
        return sorted((await conn.execute(select(ContractParent.name))).scalars().all())


# ---------------------------------------------------------------------------------------------------------
# A write auto unit is a transaction
# ---------------------------------------------------------------------------------------------------------


async def test_a_required_call_inside_a_write_method_joins_its_unit(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        parents = _repositories()
        with datasources.counter() as counter:
            await parents.register_family("ok")
        assert await _names(datasources) == ["ok", "ok-ledger"]
        assert counter.commits == 1  # one unit, one connection, one commit

        with pytest.raises(LedgerFailure):
            await parents.register_family("broken", fail=True)
        assert await _names(datasources) == ["ok", "ok-ledger"]  # the ledger's write rolled back with it


async def test_mandatory_joins_and_never_is_refused_in_a_write_method(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        parents = _repositories()
        assert await parents.register_mandatory("mandatory") is True
        with pytest.raises(IllegalTransactionStateError, match="NEVER"):
            await parents.register_never()
        assert await _names(datasources) == ["mandatory"]


async def test_nested_takes_a_savepoint_on_the_write_unit(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        await _repositories().register_with_a_nested_try("outer")
        assert await _names(datasources) == ["outer"]  # the nested write rolled back to its savepoint


async def test_a_read_method_has_no_transaction_to_join(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        parents = _repositories()
        assert await parents.find_then_record("from-a-read") is False
        assert await _names(datasources) == ["from-a-read"]  # committed by the ledger's own unit


# ---------------------------------------------------------------------------------------------------------
# Read-named write methods and decorated methods
# ---------------------------------------------------------------------------------------------------------


async def test_read_named_write_methods_commit(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        parents = _repositories()
        created = await parents.get_or_create("gc")
        again = await parents.get_or_create("gc")
        assert again.id == created.id
        await parents.find_or_create_by_name("foc")
        assert await _names(datasources) == ["foc", "gc"]


async def test_a_decorated_repository_method_runs_in_its_own_boundary(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        assert await _repositories().find_or_seed("seed") == ("seeding", False)
        assert await _names(datasources) == ["seed"]


# ---------------------------------------------------------------------------------------------------------
# expire_on_commit
# ---------------------------------------------------------------------------------------------------------


async def test_auto_units_never_expire_what_they_return(relational_backend: RelationalBackend) -> None:
    async with repository_datasources(relational_backend, *CONTRACT_MODELS) as datasources:
        expiring = async_sessionmaker(datasources.engine, expire_on_commit=True)
        managers = bind_primary_session_factory(datasources.registry, expiring)
        assert managers.sessionmaker is expiring
        parents = ParentRepository()
        saved = await parents.save(ContractParent(name="kept"))
        assert saved.name == "kept" and saved.created_at is not None  # loaded state survives the commit
        created = await parents.save_all([ContractParent(name="also")])
        assert created[0].name == "also"
        async with datasources.engine.connect() as conn:
            assert (await conn.execute(select(func.count()).select_from(ContractParent))).scalar_one() == 2
