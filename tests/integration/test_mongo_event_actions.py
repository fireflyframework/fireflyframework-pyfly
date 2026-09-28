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
"""Beanie event actions that call repositories, inside and outside a unit of work (a real replica set).

Event actions (``@before_event``/``@after_event``, ``ValidateOnSave``, BaseDocument's audit hooks and so the
application's ``AuditorAware``) are user code. Beanie runs coroutine actions in ``asyncio.gather`` child tasks,
and ``save_all`` does too for a class with two or more of them. A child task inherits the call's operation
scope, so a repository call in it waits for the unit's operation guard: a repository write that held the guard
while it ran the actions hung forever. The guard is held for one driver command at a time and never while an
action runs, so each call here answers (within :data:`TIMEOUT`), and inside a unit of work the actions' own
writes are part of it.

The query callables a post-processor subclass compiles itself (an executor's ``_compile_find`` or
``_compile_aggregate``, a legacy ``_compile_derived``) are user code too: they run without the guard, and the
framework's calls they make take it for each command.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, ClassVar, TypeVar

import pytest
from beanie import Delete, Document, Insert, Save, Update, ValidateOnSave, after_event, before_event

from pyfly.data.document.mongodb.document import BaseDocument, DocumentAuditingHandler
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.query import MongoQueryExecutor
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.query import query
from pyfly.data.transaction import IllegalTransactionStateError, TransactionTemplate
from tests.support.mongo import BeanieDatabase, beanie_database

TIMEOUT = 10.0
"""Seconds a call may take before the test calls it a hang."""

R = TypeVar("R")


class ActCustomer(Document):
    name: str

    class Settings:
        name = "act_customers"


class ActCustomerRepository(MongoRepository[ActCustomer, str]):
    pass


class ActStat(Document):
    label: str

    class Settings:
        name = "act_stats"


class ActStatRepository(MongoRepository[ActStat, str]):
    pass


class ActOrder(BaseDocument):
    """Two coroutine actions for every event it takes part in (BaseDocument's audit hook is one of them), so both
    Beanie and ``save_all`` run them in child tasks; each reads or writes another repository."""

    customer: str
    status: str = "NEW"
    seen: ClassVar[list[str]] = []

    class Settings:
        name = "act_orders"
        use_state_management = True
        validate_on_save = True

    @before_event(ValidateOnSave)
    async def customer_must_exist(self) -> None:
        if not await ActCustomerRepository().exists_by_id(self.customer):
            raise ValueError(f"unknown customer {self.customer}")

    @before_event(Insert)
    async def before_insert(self) -> None:
        ActOrder.seen.append(f"before insert: {await ActCustomerRepository().count()} customers")

    @after_event(Insert)
    async def record_insert(self) -> None:
        await ActStatRepository().save(ActStat(label=f"inserted {self.status}"))

    @after_event(Insert)
    async def after_insert(self) -> None:
        ActOrder.seen.append(f"after insert: {await ActCustomerRepository().count()} customers")

    @before_event(Save)
    async def before_save(self) -> None:
        ActOrder.seen.append(f"before save: {len(await ActCustomerRepository().find_all())} customers")

    @before_event(Update)
    async def before_update(self) -> None:
        ActOrder.seen.append(
            f"before update: customer exists {await ActCustomerRepository().exists_by_id(self.customer)}"
        )

    @after_event(Save)
    async def record_save(self) -> None:
        await ActStatRepository().save(ActStat(label=f"saved {self.status}"))

    @after_event(Save)
    async def after_save(self) -> None:
        ActOrder.seen.append(f"after save: {await ActCustomerRepository().count()} customers")

    @before_event(Delete)
    async def before_delete(self) -> None:
        ActOrder.seen.append(
            f"before delete: customer exists {await ActCustomerRepository().exists_by_id(self.customer)}"
        )

    @before_event(Delete)
    async def count_before_delete(self) -> None:
        ActOrder.seen.append(f"before delete: {await ActOrderRepository().count()} orders")

    @after_event(Delete)
    async def record_delete(self) -> None:
        await ActStatRepository().save(ActStat(label=f"deleted {self.status}"))

    @after_event(Delete)
    async def after_delete(self) -> None:
        ActOrder.seen.append(f"after delete: {await ActCustomerRepository().count()} customers")


class ActOrderRepository(MongoRepository[ActOrder, str]):
    async def delete_by_status(self, status: str) -> int: ...


class ActUser(Document):
    login: str

    class Settings:
        name = "act_users"


class ActUserRepository(MongoRepository[ActUser, str]):
    pass


class ActNote(BaseDocument):
    text: str

    class Settings:
        name = "act_notes"
        use_state_management = True


class RepositoryAuditor:
    """An ``AuditorAware`` that reads the auditor from a MongoDB repository."""

    async def get_current_auditor(self) -> str | None:
        users = await ActUserRepository().find_all(login="alice")
        return users[0].login if users else None


MODELS = [ActCustomer, ActStat, ActOrder, ActUser, ActNote]


@dataclass
class Env:
    db: BeanieDatabase
    template: TransactionTemplate
    customer: ActCustomer

    async def run(self, work: Callable[[], Awaitable[R]], *, in_unit: bool) -> R:
        """*work*, in a unit of work when *in_unit*; a call that does not answer in time fails the test."""

        async def body() -> R:
            if not in_unit:
                return await work()
            async with self.template.transaction():
                return await work()

        # Not asyncio.wait_for: a hung call may never finish its cancellation, and the test must still end.
        task = asyncio.ensure_future(body())
        done, _pending = await asyncio.wait({task}, timeout=TIMEOUT)
        if not done:
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
            pytest.fail(f"the call hung (no answer within {TIMEOUT:.0f} s)")
        return task.result()

    async def labels(self) -> list[str]:
        return sorted(row["label"] for row in await self.db.database["act_stats"].find({}).to_list())

    async def statuses(self) -> list[str]:
        return sorted(row["status"] for row in await self.db.database["act_orders"].find({}).to_list())


@pytest.fixture
async def env(mongo_rs_url: str) -> AsyncIterator[Env]:
    ActOrder.seen.clear()
    async with beanie_database(mongo_rs_url, MODELS) as db:
        customer = await ActCustomerRepository().save(ActCustomer(name="ada"))
        yield Env(db, TransactionTemplate(MongoTransactionManager.for_client(db.client)), customer)


def _orders() -> ActOrderRepository:
    repository = ActOrderRepository()
    MongoRepositoryBeanPostProcessor().after_init(repository, "actOrderRepository")
    return repository


IN_UNIT = pytest.mark.parametrize("in_unit", [False, True], ids=["auto-unit", "in-a-unit"])


@IN_UNIT
async def test_save_runs_actions_that_read_and_write_other_repositories(env: Env, in_unit: bool) -> None:
    order = await env.run(lambda: _orders().save(ActOrder(customer=str(env.customer.id))), in_unit=in_unit)
    assert order.id is not None
    assert ActOrder.seen == ["before insert: 1 customers", "after insert: 1 customers"]
    assert await env.labels() == ["inserted NEW"]
    assert await env.statuses() == ["NEW"]


@IN_UNIT
async def test_a_before_action_that_refuses_the_document_writes_nothing(env: Env, in_unit: bool) -> None:
    with pytest.raises(ValueError, match="unknown customer"):
        await env.run(lambda: _orders().save(ActOrder(customer="000000000000000000000000")), in_unit=in_unit)
    assert await env.statuses() == []
    assert await env.labels() == []


@IN_UNIT
async def test_saving_a_stored_document_runs_its_save_and_update_actions(env: Env, in_unit: bool) -> None:
    repository = _orders()
    order = await env.run(lambda: repository.save(ActOrder(customer=str(env.customer.id))), in_unit=False)
    ActOrder.seen.clear()
    order.status = "PAID"
    await env.run(lambda: repository.save(order), in_unit=in_unit)
    assert ActOrder.seen == [
        "before save: 1 customers",
        "before update: customer exists True",
        "after save: 1 customers",
    ]
    assert await env.labels() == ["inserted NEW", "saved PAID"]
    assert await env.statuses() == ["PAID"]


@IN_UNIT
async def test_save_all_runs_actions_of_many_documents_in_child_tasks(env: Env, in_unit: bool) -> None:
    repository = _orders()
    stored = await env.run(
        lambda: repository.save(ActOrder(customer=str(env.customer.id), status="OLD")), in_unit=False
    )
    stored.status = "OLDER"
    fresh = [ActOrder(customer=str(env.customer.id), status=f"N{index}") for index in range(3)]
    await env.run(lambda: repository.save_all([stored, *fresh]), in_unit=in_unit)
    assert await env.statuses() == ["N0", "N1", "N2", "OLDER"]
    assert await env.labels() == ["inserted N0", "inserted N1", "inserted N2", "inserted OLD", "saved OLDER"]


@IN_UNIT
@pytest.mark.parametrize("how", ["delete", "delete_by_id", "delete_all", "delete_all_by_id", "delete_by_status"])
async def test_every_delete_runs_actions_that_call_other_repositories(env: Env, in_unit: bool, how: str) -> None:
    repository = _orders()
    order = await env.run(
        lambda: repository.save(ActOrder(customer=str(env.customer.id), status="GONE")), in_unit=False
    )
    ActOrder.seen.clear()
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "delete": lambda: repository.delete(order),
        "delete_by_id": lambda: repository.delete_by_id(str(order.id)),
        "delete_all": lambda: repository.delete_all([order]),
        "delete_all_by_id": lambda: repository.delete_all_by_id([str(order.id)]),
        "delete_by_status": lambda: repository.delete_by_status("GONE"),
    }
    await env.run(calls[how], in_unit=in_unit)
    assert await env.statuses() == []
    assert sorted(ActOrder.seen) == [
        "after delete: 1 customers",
        "before delete: 1 orders",
        "before delete: customer exists True",
    ]
    assert await env.labels() == ["deleted GONE", "inserted GONE"]


async def test_the_writes_of_event_actions_are_part_of_the_unit_of_work(env: Env) -> None:
    """Inside a unit the actions' own writes join it: a failure after the save undoes all of them."""
    repository = _orders()

    async def work() -> None:
        await repository.save(ActOrder(customer=str(env.customer.id)))
        order = await repository.save(ActOrder(customer=str(env.customer.id), status="SECOND"))
        await repository.delete(order)
        raise RuntimeError("rolled back")

    with pytest.raises(RuntimeError, match="rolled back"):
        await env.run(work, in_unit=True)
    assert await env.statuses() == []
    assert await env.labels() == []


@IN_UNIT
async def test_an_auditor_read_from_a_repository_stamps_every_write(env: Env, in_unit: bool) -> None:
    await env.db.database["act_users"].insert_one({"login": "alice"})
    handler = DocumentAuditingHandler(auditor_aware=RepositoryAuditor())
    await handler.start()
    try:
        notes: MongoRepository[ActNote, str] = MongoRepository(ActNote)
        note = await env.run(lambda: notes.save(ActNote(text="one")), in_unit=in_unit)
        assert (note.created_by, note.updated_by) == ("alice", "alice")
        note.text = "changed"
        await env.run(lambda: notes.save(note), in_unit=in_unit)
        batch = await env.run(lambda: notes.save_all([ActNote(text="two"), ActNote(text="three")]), in_unit=in_unit)
        assert all(saved.created_by == "alice" for saved in batch)
        rows = await env.db.database["act_notes"].find({}).to_list()
        assert sorted((row["text"], row["created_by"], row["updated_by"]) for row in rows) == [
            ("changed", "alice", "alice"),
            ("three", "alice", "alice"),
            ("two", "alice", "alice"),
        ]
    finally:
        await handler.stop()


@pytest.mark.parametrize(
    "how", ["save new", "save stored", "save_all", "delete", "delete_by_id", "delete_all", "delete_by_status"]
)
async def test_a_read_only_unit_refuses_a_write_before_any_event_action_runs(env: Env, how: str) -> None:
    """A write in a read-only unit fails before the document's validation and event actions (user code) run."""
    repository = _orders()
    order = await env.run(
        lambda: repository.save(ActOrder(customer=str(env.customer.id), status="KEPT")), in_unit=False
    )
    ActOrder.seen.clear()
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "save new": lambda: repository.save(ActOrder(customer=str(env.customer.id))),
        "save stored": lambda: repository.save(order),
        "save_all": lambda: repository.save_all([order, ActOrder(customer=str(env.customer.id))]),
        "delete": lambda: repository.delete(order),
        "delete_by_id": lambda: repository.delete_by_id(str(order.id)),
        "delete_all": lambda: repository.delete_all([order]),
        "delete_by_status": lambda: repository.delete_by_status("KEPT"),
    }

    async def read_only() -> None:
        async with env.template.transaction(read_only=True):
            await calls[how]()

    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        await env.run(read_only, in_unit=False)
    assert ActOrder.seen == []
    assert await env.statuses() == ["KEPT"]
    assert await env.labels() == ["inserted KEPT"]


async def _customers_in_a_child_task() -> int:
    """User code that reads a repository in a child task (``asyncio.gather`` runs its coroutine in one)."""
    (customers,) = await asyncio.gather(ActCustomerRepository().count())
    return int(customers)


class ChildTaskQueries(MongoQueryExecutor):
    """An executor whose find filters compile to a coroutine of its own."""

    def _compile_find(self, query_string: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        async def compiled(model: type, **kwargs: Any) -> int:
            return await _customers_in_a_child_task()

        return compiled


class ChildTaskProcessor(MongoRepositoryBeanPostProcessor):
    """A processor with an executor of its own, and derived queries compiled by its own legacy hook."""

    def __init__(self) -> None:
        super().__init__()
        self._query_executor = ChildTaskQueries()

    def _compile_derived(self, parsed: Any, entity: Any, bean: Any, *, return_type: Any = None) -> Any:
        async def compiled(model: type, *args: Any) -> int:
            return await _customers_in_a_child_task()

        return compiled


class ActHookedQueries(MongoRepository[ActOrder, str]):
    @query('{"status": ":status"}')
    async def find_hooked(self, status: str) -> int: ...

    async def count_by_status(self, status: str) -> int: ...


@IN_UNIT
@pytest.mark.parametrize("method", ["find_hooked", "count_by_status"])
async def test_query_code_a_processor_subclass_compiles_calls_repositories_in_child_tasks(
    env: Env, in_unit: bool, method: str
) -> None:
    repository = ActHookedQueries()
    ChildTaskProcessor().after_init(repository, "actHookedQueries")
    assert await env.run(lambda: getattr(repository, method)("NEW"), in_unit=in_unit) == 1
