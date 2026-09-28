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
"""MongoDB transactions on the unit of work, proven on a real replica set (MongoDB 7, ``rs0``).

``@transactional`` on a document service used to crash on every call (C041: ``start_transaction()`` is a
coroutine on pymongo's async API), and repository writes never carried the ``ClientSession``, so they ran
outside the transaction (F14). Every test here writes through a repository or Beanie and reads the result
back on another client, so a write that escaped its transaction shows.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from beanie import Indexed, init_beanie
from pymongo import AsyncMongoClient

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager, current_session
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    NestedTransactionNotSupportedError,
    Propagation,
    TransactionManagerRegistry,
    TransactionTemplate,
    TransactionTimedOutError,
    UnexpectedRollbackError,
    after_commit,
    current_unit_of_work,
    infrastructure_unit,
    install_registry,
    is_transaction_active,
    transactional,
    uninstall_registry,
)
from pyfly.kernel.exceptions import ConcurrencyException, DuplicateKeyException
from tests.support.backend_matrix import MongoBackend
from tests.support.mongo import beanie_database


class TxAccount(BaseDocument):
    owner: Indexed(str, unique=True)  # type: ignore[valid-type]
    balance: int = 0

    class Settings:
        name = "tx_accounts"


class TxAccountRepository(MongoRepository[TxAccount, str]):
    pass


@dataclass
class Env:
    client: AsyncMongoClient[Any]
    manager: MongoTransactionManager
    repository: TxAccountRepository
    url: str
    database: str

    async def owners(self) -> list[str]:
        """The owners committed, read on a client of its own (outside every session)."""
        reader: AsyncMongoClient[Any] = AsyncMongoClient(self.url)
        try:
            rows = await reader[self.database]["tx_accounts"].find({}, {"owner": 1}).sort("owner", 1).to_list()
            return [row["owner"] for row in rows]
        finally:
            await reader.close()


@pytest.fixture
async def env(mongo_backend: MongoBackend) -> AsyncIterator[Env]:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
    await init_beanie(database=client[mongo_backend.database], document_models=[TxAccount])
    manager = MongoTransactionManager(client)
    registry = TransactionManagerRegistry(default=manager.datasource)
    registry.register(manager)
    install_registry(registry)
    try:
        yield Env(client, manager, TxAccountRepository(), mongo_backend.url, mongo_backend.database)
    finally:
        uninstall_registry(registry)
        await client.close()


class LegacyService:
    """A document service written against the documented contract: ``self._motor_client`` selects Mongo."""

    def __init__(self, client: AsyncMongoClient[Any], repository: TxAccountRepository) -> None:
        self._motor_client = client
        self.repository = repository

    @transactional
    async def open_two(self, first: str, second: str) -> None:
        await self.repository.save(TxAccount(owner=first))
        await self.repository.save(TxAccount(owner=second))

    @transactional
    async def open_then_fail(self, owner: str) -> None:
        await self.repository.save(TxAccount(owner=owner))
        raise ValueError("boom")

    @transactional
    async def with_session(self, owner: str, *, session: Any = None) -> Any:
        await TxAccount(owner=owner).insert(session=session)
        return session


# ---------------------------------------------------------------------------------------------------------
# C041 / F14: commit, abort, the session reaches every write
# ---------------------------------------------------------------------------------------------------------


async def test_a_legacy_document_service_commits_its_writes(env: Env) -> None:
    """C041: ``@transactional`` on a ``_motor_client`` service crashed on every call."""
    await LegacyService(env.client, env.repository).open_two("ada", "bob")
    assert await env.owners() == ["ada", "bob"]


async def test_a_failure_rolls_back_a_real_repository_write(env: Env) -> None:
    """F14: the repository write carries the unit's ClientSession, so the abort undoes it."""
    with pytest.raises(ValueError, match="boom"):
        await LegacyService(env.client, env.repository).open_then_fail("carol")
    assert await env.owners() == []


async def test_a_session_parameter_still_receives_the_units_session(env: Env) -> None:
    """The documented contract: a ``session`` keyword gets the live ClientSession of the unit."""
    service = LegacyService(env.client, env.repository)
    session = await service.with_session("dora")
    assert session is not None and session.has_ended
    assert await env.owners() == ["dora"]


async def test_the_unit_binds_the_session_for_the_repository(env: Env) -> None:
    @transactional(datasource="document")
    async def work() -> tuple[Any, Any]:
        unit = current_unit_of_work("document")
        assert unit is not None
        await env.repository.save(TxAccount(owner="erin"))
        # Not visible outside the transaction before it commits.
        assert await env.owners() == []
        return unit.resource, unit.resource.in_transaction

    session, in_transaction = await work()
    assert in_transaction is True
    assert await env.owners() == ["erin"]


# ---------------------------------------------------------------------------------------------------------
# Propagation
# ---------------------------------------------------------------------------------------------------------


async def test_required_joins_the_outer_unit(env: Env) -> None:
    @transactional(datasource="document")
    async def inner() -> None:
        await env.repository.save(TxAccount(owner="inner"))

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="outer"))
        await inner()
        raise ValueError("outer fails")

    with pytest.raises(ValueError):
        await outer()
    assert await env.owners() == []


async def test_requires_new_commits_on_its_own_and_resumes_the_outer_unit(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.REQUIRES_NEW)
    async def audit() -> None:
        await env.repository.save(TxAccount(owner="audit"))

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="before"))
        await audit()
        await env.repository.save(TxAccount(owner="after"))
        raise ValueError("outer fails")

    with pytest.raises(ValueError):
        await outer()
    assert await env.owners() == ["audit"]


async def test_nested_inside_a_unit_is_refused(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.NESTED)
    async def nested() -> None:
        await env.repository.save(TxAccount(owner="nested"))

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="outer"))
        await nested()

    with pytest.raises(NestedTransactionNotSupportedError):
        await outer()
    assert await env.owners() == []


async def test_nested_without_a_unit_starts_a_transaction(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.NESTED)
    async def nested() -> None:
        await env.repository.save(TxAccount(owner="n1"))
        raise ValueError("no")

    with pytest.raises(ValueError):
        await nested()
    assert await env.owners() == []


async def test_supports_joins_a_unit_and_runs_without_one_otherwise(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.SUPPORTS)
    async def maybe(owner: str) -> bool:
        await env.repository.save(TxAccount(owner=owner))
        return is_transaction_active("document")

    @transactional(datasource="document")
    async def outer() -> None:
        assert await maybe("joined") is True
        raise ValueError("outer fails")

    with pytest.raises(ValueError):
        await outer()
    assert await maybe("alone") is False  # the save ran (and committed) in a short unit of its own
    assert await env.owners() == ["alone"]


async def test_not_supported_suspends_the_unit(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.NOT_SUPPORTED)
    async def outside() -> bool:
        await env.repository.save(TxAccount(owner="outside"))
        return is_transaction_active("document")

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="inside"))
        assert await outside() is False
        assert is_transaction_active("document")
        raise ValueError("outer fails")

    with pytest.raises(ValueError):
        await outer()
    assert await env.owners() == ["outside"]


async def test_mandatory_needs_a_unit_and_never_refuses_one(env: Env) -> None:
    @transactional(datasource="document", propagation=Propagation.MANDATORY)
    async def mandatory() -> None:
        await env.repository.save(TxAccount(owner="mandatory"))

    @transactional(datasource="document", propagation=Propagation.NEVER)
    async def never() -> None:
        await env.repository.save(TxAccount(owner="never"))

    @transactional(datasource="document")
    async def outer_mandatory() -> None:
        await mandatory()

    @transactional(datasource="document")
    async def outer_never() -> None:
        await never()

    with pytest.raises(IllegalTransactionStateError, match="MANDATORY"):
        await mandatory()
    await outer_mandatory()
    with pytest.raises(IllegalTransactionStateError, match="NEVER"):
        await outer_never()
    await never()
    assert await env.owners() == ["mandatory", "never"]


# ---------------------------------------------------------------------------------------------------------
# Rollback-only, statement failures, translation
# ---------------------------------------------------------------------------------------------------------


async def test_a_caught_participant_failure_rolls_the_unit_back(env: Env) -> None:
    @transactional(datasource="document")
    async def participant() -> None:
        await env.repository.save(TxAccount(owner="participant"))
        raise ValueError("participant fails")

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="outer"))
        with contextlib.suppress(ValueError):
            await participant()

    with pytest.raises(UnexpectedRollbackError):
        await outer()
    assert await env.owners() == []


async def test_a_caught_duplicate_key_dooms_the_unit(env: Env) -> None:
    """The server aborts a transaction whose statement failed: a caught failure cannot commit the rest."""
    await env.repository.save(TxAccount(owner="taken"))

    @transactional(datasource="document")
    async def outer() -> None:
        await env.repository.save(TxAccount(owner="fresh"))
        with pytest.raises(DuplicateKeyException):
            await env.repository.save(TxAccount(owner="taken"))

    with pytest.raises(UnexpectedRollbackError):
        await outer()
    assert await env.owners() == ["taken"]


async def test_a_write_conflict_is_a_concurrency_exception(env: Env) -> None:
    saved = await env.repository.save(TxAccount(owner="shared", balance=1))
    first_wrote = asyncio.Event()
    release_first = asyncio.Event()

    @transactional(datasource="document")
    async def first() -> None:
        account = await env.repository.find_by_id(saved.id)
        assert account is not None
        account.balance = 2
        await env.repository.save(account)
        first_wrote.set()
        await release_first.wait()

    @transactional(datasource="document")
    async def second() -> None:
        await first_wrote.wait()
        account = await env.repository.find_by_id(saved.id)
        assert account is not None
        account.balance = 3
        await env.repository.save(account)

    first_task = asyncio.create_task(first())
    try:
        with pytest.raises(ConcurrencyException):
            await second()
    finally:
        release_first.set()
        await first_task
    reloaded = await env.repository.find_by_id(saved.id)
    assert reloaded is not None and reloaded.balance == 2


async def test_gather_inside_a_unit_is_serialized_and_atomic(env: Env) -> None:
    @transactional(datasource="document")
    async def fan_out(fail: bool) -> None:
        await asyncio.gather(*(env.repository.save(TxAccount(owner=f"g{index}")) for index in range(8)))
        if fail:
            raise ValueError("after the fan-out")

    with pytest.raises(ValueError):
        await fan_out(True)
    assert await env.owners() == []
    await fan_out(False)
    assert await env.owners() == [f"g{index}" for index in range(8)]


async def test_a_read_only_unit_refuses_repository_writes(env: Env) -> None:
    @transactional(datasource="document", read_only=True)
    async def reader() -> None:
        await env.repository.save(TxAccount(owner="nope"))

    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        await reader()
    assert await env.owners() == []


# ---------------------------------------------------------------------------------------------------------
# Timeout, cancellation, synchronizations, programmatic use
# ---------------------------------------------------------------------------------------------------------


async def test_a_timeout_rolls_back(env: Env) -> None:
    @transactional(datasource="document", timeout=0.2)
    async def slow() -> None:
        await env.repository.save(TxAccount(owner="slow"))
        await asyncio.sleep(5)

    with pytest.raises(TransactionTimedOutError):
        await slow()
    assert await env.owners() == []


async def test_a_cancelled_unit_is_aborted_and_releases_its_locks(env: Env) -> None:
    """A client disconnect in mid-transaction must abort it: a later write on the same document succeeds at
    once instead of meeting a write conflict with a transaction left open on the server."""
    saved = await env.repository.save(TxAccount(owner="locked", balance=1))

    @transactional(datasource="document")
    async def hangs() -> None:
        account = await env.repository.find_by_id(saved.id)
        assert account is not None
        account.balance = 99
        await env.repository.save(account)
        await asyncio.sleep(30)

    with anyio.move_on_after(0.3):
        await hangs()

    @transactional(datasource="document")
    async def writes() -> None:
        account = await env.repository.find_by_id(saved.id)
        assert account is not None
        account.balance = 5
        await env.repository.save(account)

    await writes()
    reloaded = await env.repository.find_by_id(saved.id)
    assert reloaded is not None and reloaded.balance == 5


async def test_after_commit_runs_only_after_a_commit(env: Env) -> None:
    seen: list[str] = []

    async def record() -> None:
        seen.append("committed")

    @transactional(datasource="document")
    async def work(fail: bool) -> None:
        await env.repository.save(TxAccount(owner=f"sync-{fail}"))
        await after_commit(record)
        assert seen == []
        if fail:
            raise ValueError("rolled back")

    with pytest.raises(ValueError):
        await work(True)
    assert seen == []
    await work(False)
    assert seen == ["committed"]


async def test_the_template_and_infrastructure_units_run_on_the_document_datasource(env: Env) -> None:
    template = TransactionTemplate("document")
    async with template.transaction() as unit:
        assert unit is not None
        async with infrastructure_unit("document") as session:
            assert session is unit.resource  # joined
            await env.client[env.database]["tx_accounts"].insert_one({"owner": "raw", "balance": 0}, session=session)
    async with infrastructure_unit("document") as own:
        assert own is not None and own is not unit.resource
    assert await env.owners() == ["raw"]


async def test_the_default_datasource_runs_a_plain_function(env: Env) -> None:
    @transactional
    async def plain() -> None:
        await env.repository.save(TxAccount(owner="plain"))
        raise ValueError("rolled back")

    with pytest.raises(ValueError):
        await plain()
    assert await env.owners() == []


# ---------------------------------------------------------------------------------------------------------
# A standalone server has no transactions
# ---------------------------------------------------------------------------------------------------------


async def test_a_standalone_server_refuses_a_transaction_clearly_and_still_serves_repositories(
    mongo_url: str,
) -> None:
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_url)
    database = f"pyfly_standalone_{id(client):x}"
    try:
        await init_beanie(database=client[database], document_models=[TxAccount])
        manager = MongoTransactionManager(client)
        repository = TxAccountRepository()

        @transactional(manager=manager)
        async def work() -> None:
            await repository.save(TxAccount(owner="never"))

        with pytest.raises(IllegalTransactionStateError, match="replica set"):
            await work()
        # Outside a transaction the repository needs none: each write is its own, as on a replica set.
        await repository.save(TxAccount(owner="plain"))
        assert [account.owner for account in await repository.find_all()] == ["plain"]
    finally:
        await client.drop_database(database)
        await client.close()


async def test_current_session_serves_code_that_calls_beanie_itself(env: Env) -> None:
    assert current_session() is None

    @transactional(datasource="document")
    async def raw_write() -> None:
        session = current_session()
        unit = current_unit_of_work("document")
        assert unit is not None and session is unit.resource
        assert current_session("document") is session
        await TxAccount(owner="raw").insert(session=current_session())
        raise ValueError("rolled back with the unit")

    with pytest.raises(ValueError):
        await raw_write()
    assert await env.owners() == []


async def test_every_command_of_a_unit_carries_its_transaction(mongo_backend: MongoBackend) -> None:
    """F14: the session reaches every driver call, terminal ones included (find, count, aggregate, bulk write,
    delete): each command inside the unit carries the transaction number of the unit's session."""
    async with beanie_database(mongo_backend.url, [TxAccount]) as db:
        manager = MongoTransactionManager(db.client)
        repository = TxAccountRepository()
        from pyfly.data.pageable import Order, Pageable, Sort

        @transactional(manager=manager)
        async def everything() -> None:
            saved = await repository.save(TxAccount(owner="a"))
            batch = await repository.save_all([TxAccount(owner="b"), TxAccount(owner="c")])
            batch[0].balance = 5
            await repository.save_all(batch)  # an update
            saved.balance = 1
            await repository.save(saved)  # Beanie's save of a stored document: findAndModify
            await repository.find_by_id(saved.id)
            await repository.find_all_by_id([saved.id])
            await repository.exists_by_id(saved.id)
            await repository.count()
            await repository.find_all(Pageable.of(1, 2, Sort.by("owner")))
            await repository.find_all(Sort.by(Order.asc("owner").ignoring_case()))
            await repository.find_slice(Pageable.of(1, 2))
            _ = [account async for account in repository.stream_all()]
            await repository.delete(saved)
            await repository.delete_all_by_id([saved.id])
            await repository.delete_all_in_batch()

        db.log.clear()
        await everything()
        commands = [(name, body) for name, body in db.log.commands if name != "commitTransaction"]
        assert {name for name, _body in commands} >= {
            "insert",
            "update",
            "findAndModify",
            "find",
            "aggregate",
            "delete",
        }
        transactions = {body.get("txnNumber") for _name, body in commands}
        assert len(transactions) == 1 and None not in transactions
        assert db.log.names()[-1] == "commitTransaction"
