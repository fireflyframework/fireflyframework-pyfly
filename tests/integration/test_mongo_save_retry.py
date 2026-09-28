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
"""A failed ``save`` or ``save_all`` leaves its documents as the server holds them, so saving the same objects again
works (a real replica set, and a standalone server for the writes that run without a transaction).

``save_all`` makes the ids of new documents and the revisions of every document on the client, before its bulk
write, and ``save`` does for a new document. When the write fails, or the unit of work it ran in rolls back, the
documents get back the id, revision and saved state they had: a retry of a ``ConcurrencyException`` (a write
conflict) or of a fixed ``DuplicateKeyException`` saves them, instead of raising
``OptimisticLockingFailureException`` for a revision the server never stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import uuid
import weakref
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from beanie import Document, Insert, Save, after_event, before_event, init_beanie
from beanie.odm.fields import PydanticObjectId
from pydantic import ConfigDict, field_validator
from pymongo import AsyncMongoClient, IndexModel, WriteConcern
from pymongo.errors import WriteConcernError

from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import CommitOutcomeUnknownError, Propagation, TransactionTemplate
from pyfly.data.transaction.synchronization import TransactionPhase, on_phase
from pyfly.kernel.exceptions import ConcurrencyException, DuplicateKeyException, OptimisticLockingFailureException
from tests.support.mongo import BeanieDatabase, beanie_database


class RtDoc(Document):
    code: str
    n: int = 0
    journal: ClassVar[list[str]] = []
    refused: ClassVar[bool] = False

    class Settings:
        name = "rt_docs"
        use_revision = True
        use_state_management = True
        indexes = [IndexModel("code", unique=True)]

    @before_event(Insert)
    def refuse(self) -> None:
        if RtDoc.refused:
            raise ValueError("insert refused")

    @after_event(Insert)
    def inserted(self) -> None:
        RtDoc.journal.append(f"insert {self.code}")

    @after_event(Save)
    def saved(self) -> None:
        RtDoc.journal.append(f"save {self.code}")


class RtParked(Document):
    """A document whose insert action waits at :attr:`gate` while one is set."""

    code: str
    gate: ClassVar[asyncio.Event | None] = None
    parked: ClassVar[asyncio.Event | None] = None

    class Settings:
        name = "rt_parked"
        use_revision = True

    @before_event(Insert)
    async def park(self) -> None:
        if RtParked.gate is not None and RtParked.parked is not None:
            RtParked.parked.set()
            await RtParked.gate.wait()


class RtRepository(MongoRepository[RtDoc, str]):
    async def import_then_fail(self, documents: list[RtDoc]) -> None:
        """Saves *documents*, then fails: outside a transaction the method's own unit rolls the save back."""
        await self.save_all(documents)
        raise RuntimeError("after the save")


@pytest.fixture
async def db(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    RtDoc.journal.clear()
    async with beanie_database(mongo_rs_url, [RtDoc, RtParked]) as database:
        yield database


@pytest.fixture
def template(db: BeanieDatabase) -> TransactionTemplate:
    return TransactionTemplate(MongoTransactionManager.for_client(db.client))


async def _assert_as_stored(*documents: RtDoc) -> None:
    """Each document's id and revision are the ones the server holds (a document that is not stored has none)."""
    for document in documents:
        assert document.id is not None, f"{document.code} has no id"
        stored = await RtDoc.get(document.id)
        assert stored is not None, f"{document.code} is not stored"
        assert document.revision_id == stored.revision_id, f"{document.code} holds a revision the server does not"


def _assert_new(*documents: RtDoc) -> None:
    for document in documents:
        assert (document.id, document.revision_id) == (None, None), f"{document.code} kept the id of a failed insert"


async def _stored_codes(db: BeanieDatabase) -> dict[str, int]:
    return {row["code"]: row["n"] for row in await db.database["rt_docs"].find({}).to_list()}


IN_UNIT = pytest.mark.parametrize("in_unit", [False, True], ids=["auto-unit", "in-a-unit"])


async def _save_all(template: TransactionTemplate, documents: list[RtDoc], *, in_unit: bool) -> Any:
    repository = RtRepository()
    if not in_unit:
        return await repository.save_all(documents)
    async with template.transaction():
        return await repository.save_all(documents)


@IN_UNIT
async def test_save_all_is_retried_after_a_write_conflict(
    db: BeanieDatabase, template: TransactionTemplate, in_unit: bool
) -> None:
    repository = RtRepository()
    first, second = await repository.save_all([RtDoc(code="a"), RtDoc(code="b")])
    other = db.client.start_session()
    try:
        # Another open transaction holds a write on the second document: the batch's update of it conflicts.
        await other.start_transaction()
        await db.database["rt_docs"].update_one({"_id": second.id}, {"$set": {"n": 99}}, session=other)
        first.n = second.n = 1
        with pytest.raises(ConcurrencyException) as raised:
            await _save_all(template, [first, second], in_unit=in_unit)
        assert not isinstance(raised.value, OptimisticLockingFailureException)
        await other.abort_transaction()
    finally:
        await other.end_session()
    await _assert_as_stored(first, second)
    assert await _stored_codes(db) == {"a": 0, "b": 0}

    await repository.save_all([first, second])
    assert await _stored_codes(db) == {"a": 1, "b": 1}
    first.n = 2
    await repository.save(first)
    await _assert_as_stored(first, second)
    assert await _stored_codes(db) == {"a": 2, "b": 1}


@IN_UNIT
async def test_save_all_is_retried_after_a_duplicate_key(
    db: BeanieDatabase, template: TransactionTemplate, in_unit: bool
) -> None:
    repository = RtRepository()
    stored = await repository.save(RtDoc(code="a"))
    await repository.save(RtDoc(code="taken"))
    stored.n = 1
    duplicate = RtDoc(code="taken")
    with pytest.raises(DuplicateKeyException):
        await _save_all(template, [stored, duplicate], in_unit=in_unit)
    await _assert_as_stored(stored)
    _assert_new(duplicate)

    duplicate.code = "b"
    RtDoc.journal.clear()
    await repository.save_all([stored, duplicate])
    assert RtDoc.journal == ["save a", "insert b"]  # the fixed document takes the insert path again
    await repository.save(stored)
    await repository.save(duplicate)
    await _assert_as_stored(stored, duplicate)
    assert await _stored_codes(db) == {"a": 1, "taken": 0, "b": 0}


async def test_save_of_a_new_document_is_retried_as_an_insert(db: BeanieDatabase) -> None:
    repository = RtRepository()
    await repository.save(RtDoc(code="taken"))
    document = RtDoc(code="taken")
    with pytest.raises(DuplicateKeyException):
        await repository.save(document)
    _assert_new(document)

    document.code = "c"
    RtDoc.journal.clear()
    await repository.save(document)
    assert RtDoc.journal == ["insert c"]
    await _assert_as_stored(document)


async def test_a_unit_that_rolls_back_gives_its_documents_back_the_stored_state(
    db: BeanieDatabase, template: TransactionTemplate
) -> None:
    repository = RtRepository()
    stored = await repository.save(RtDoc(code="a"))
    fresh = RtDoc(code="fresh")
    with pytest.raises(RuntimeError, match="rolled back"):
        async with template.transaction():
            stored.n = 5
            await repository.save(stored)
            await repository.save_all([stored, fresh])
            raise RuntimeError("rolled back")
    await _assert_as_stored(stored)
    assert stored.is_changed  # n=5 is not stored: the saved state is the stored one again
    _assert_new(fresh)

    await repository.save_all([stored, fresh])
    await _assert_as_stored(stored, fresh)
    assert await _stored_codes(db) == {"a": 5, "fresh": 0}
    assert not stored.is_changed


async def test_a_rollback_after_save_all_returned_gives_the_documents_back(db: BeanieDatabase) -> None:
    """Outside a transaction a subclass method that writes runs in a unit of its own: a failure after its
    ``save_all`` returned rolls the save back, and the documents get their stored state back."""
    repository = RtRepository()
    stored = await repository.save(RtDoc(code="a"))
    stored.n = 3
    fresh = RtDoc(code="fresh")
    with pytest.raises(RuntimeError, match="after the save"):
        await repository.import_then_fail([stored, fresh])
    await _assert_as_stored(stored)
    _assert_new(fresh)
    assert await _stored_codes(db) == {"a": 0}

    await repository.save_all([stored, fresh])
    await _assert_as_stored(stored, fresh)
    assert await _stored_codes(db) == {"a": 3, "fresh": 0}


async def test_a_cancelled_save_all_gives_the_documents_back(db: BeanieDatabase) -> None:
    """A cancellation is a failure too: cancelled while an insert action runs, after the batch made a revision for
    the stored document and an id for the new one, ``save_all`` gives both back."""
    repository: MongoRepository[RtParked, str] = MongoRepository(RtParked)
    stored = await repository.save(RtParked(code="a"))
    stored.code = "b"
    fresh = RtParked(code="fresh")
    RtParked.gate, RtParked.parked = asyncio.Event(), asyncio.Event()
    try:
        task = asyncio.ensure_future(repository.save_all([stored, fresh]))
        await RtParked.parked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        RtParked.gate = RtParked.parked = None
    found = await RtParked.get(stored.id)
    assert found is not None and (found.code, found.revision_id) == ("a", stored.revision_id)
    assert (fresh.id, fresh.revision_id) == (None, None)

    await repository.save_all([stored, fresh])
    assert sorted(row["code"] for row in await db.database["rt_parked"].find({}).to_list()) == ["b", "fresh"]


@pytest.mark.parametrize("stored_first", [False, True], ids=["new", "stored"])
async def test_an_after_rollback_callback_that_saves_the_document_again_keeps_what_it_stored(
    db: BeanieDatabase, template: TransactionTemplate, stored_first: bool
) -> None:
    """The documents get their state back before any after-rollback callback of the unit runs, even one registered
    before its first save: a callback that saves the document again in a unit of its own keeps what it stored."""
    repository = RtRepository()
    order = RtDoc(code="o1")
    if stored_first:
        await repository.save(order)
    errors: list[BaseException] = []

    async def mark_failed() -> None:
        order.n = -1
        try:
            await repository.save(order)
        except Exception as error:  # noqa: BLE001 — a callback's failure is logged, never raised: keep it to assert
            errors.append(error)

    with pytest.raises(RuntimeError, match="business failure"):
        async with template.transaction():
            await on_phase(TransactionPhase.AFTER_ROLLBACK, mark_failed)
            order.n = 1
            await repository.save(order)
            raise RuntimeError("business failure")
    assert errors == []
    await _assert_as_stored(order)
    assert await _stored_codes(db) == {"o1": -1}

    order.n = 2
    await repository.save(order)
    await _assert_as_stored(order)
    assert await _stored_codes(db) == {"o1": 2}
    assert await db.database["rt_docs"].count_documents({}) == 1


async def test_a_save_that_failed_before_its_write_leaves_a_later_write_of_another_unit_alone(
    db: BeanieDatabase, template: TransactionTemplate
) -> None:
    """Only a write the unit made is undone by its rollback: a save that failed before writing, followed by a save of
    the same document in a unit of its own that commits, leaves the document as that unit stored it."""
    repository = RtRepository()
    item = RtDoc(code="b")
    with pytest.raises(RuntimeError, match="outer fails later"):
        async with template.transaction():
            RtDoc.refused = True
            try:
                with pytest.raises(ValueError, match="insert refused"):
                    await repository.save(item)
            finally:
                RtDoc.refused = False
            async with template.transaction(propagation=Propagation.REQUIRES_NEW):
                await repository.save(item)
            raise RuntimeError("outer fails later")
    await _assert_as_stored(item)

    item.n = 3
    await repository.save(item)
    assert await _stored_codes(db) == {"b": 3}
    assert await db.database["rt_docs"].count_documents({}) == 1


class RtUnacknowledged(Document):
    code: str

    class Settings:
        name = "rt_unacknowledged"
        use_revision = True


async def test_a_new_document_whose_write_concern_fails_keeps_the_id_it_was_stored_with(mongo_rs_url: str) -> None:
    """``w: 2`` on the one-member replica set: the insert is applied, and only its acknowledgment by a second member
    fails (``WriteConcernError``, not translated: nothing is wrong with the data). The document keeps the id and
    revision it was stored with, so saving it again updates it instead of inserting it a second time."""
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_rs_url)
    name = f"pyfly_t_{uuid.uuid4().hex[:12]}"
    try:
        unacknowledged = client.get_database(name, write_concern=WriteConcern(w=2, wtimeout=500))
        await init_beanie(database=unacknowledged, document_models=[RtUnacknowledged])
        repository: MongoRepository[RtUnacknowledged, str] = MongoRepository(RtUnacknowledged)
        document = RtUnacknowledged(code="order-1")
        with pytest.raises(WriteConcernError):
            await repository.save(document)
        rows = await client[name]["rt_unacknowledged"].find({}).to_list()
        assert [row["_id"] for row in rows] == [document.id]
        stored = await RtUnacknowledged.get(PydanticObjectId(document.id))
        assert stored is not None and stored.revision_id == document.revision_id

        with pytest.raises(WriteConcernError):
            await repository.save(document)
        assert await client[name]["rt_unacknowledged"].count_documents({}) == 1
    finally:
        await client.drop_database(name)
        await client.close()


async def test_a_write_concern_failure_at_commit_keeps_what_the_transaction_stored(mongo_rs_url: str) -> None:
    """``w: 2`` for the client on the one-member replica set: a transaction's writes are applied, and its commit's
    write concern fails (``UnsatisfiableWriteConcern``, which pymongo does not label an unknown commit result). The
    commit may have applied, so the boundary raises ``CommitOutcomeUnknownError`` and the documents keep the ids and
    revisions they were written with: a retry updates them, never inserts them a second time."""
    client: AsyncMongoClient[Any] = AsyncMongoClient(mongo_rs_url, w=2, wtimeoutMS=500)
    name = f"pyfly_t_{uuid.uuid4().hex[:12]}"
    try:
        await init_beanie(database=client[name], document_models=[RtUnacknowledged])
        repository: MongoRepository[RtUnacknowledged, str] = MongoRepository(RtUnacknowledged)
        collection = client[name]["rt_unacknowledged"]

        first = RtUnacknowledged(code="a")
        with pytest.raises(CommitOutcomeUnknownError):
            await repository.save_all([first])  # outside a unit: its own transaction
        assert first.id is not None and await collection.count_documents({}) == 1
        stored = await RtUnacknowledged.get(PydanticObjectId(first.id))
        assert stored is not None and stored.revision_id == first.revision_id
        with pytest.raises(CommitOutcomeUnknownError):
            await repository.save_all([first])
        assert await collection.count_documents({}) == 1

        second = RtUnacknowledged(code="b")
        with pytest.raises(CommitOutcomeUnknownError):
            async with TransactionTemplate(MongoTransactionManager.for_client(client)).transaction():
                await repository.save(second)
        assert second.id is not None
        stored = await RtUnacknowledged.get(PydanticObjectId(second.id))
        assert stored is not None and stored.revision_id == second.revision_id
        assert await collection.count_documents({}) == 2
    finally:
        await client.close()
        cleaner: AsyncMongoClient[Any] = AsyncMongoClient(mongo_rs_url)  # dropDatabase takes the write concern too
        await cleaner.drop_database(name)
        await cleaner.close()


@pytest.mark.parametrize("fail", [False, True], ids=["committed", "rolled-back"])
async def test_a_completed_unit_keeps_no_document_alive(
    db: BeanieDatabase, template: TransactionTemplate, fail: bool
) -> None:
    repository = RtRepository()
    documents = [RtDoc(code=f"d{index}") for index in range(3)]
    references = [weakref.ref(document) for document in documents]
    units = []
    with contextlib.suppress(RuntimeError):
        async with template.transaction():
            units.append(repository._current_unit())
            await repository.save(documents[0])
            await repository.save_all(documents[1:])
            if fail:
                raise RuntimeError("rolled back")
    del documents
    gc.collect()
    assert units[0].completed
    assert [reference() for reference in references] == [None, None, None]


class RtGuarded(Document):
    """A document whose id, once given, cannot be taken back (its validator runs on assignment)."""

    model_config = ConfigDict(validate_assignment=True)

    code: str

    class Settings:
        name = "rt_guarded"
        use_revision = True

    @field_validator("id")
    @classmethod
    def _kept(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("an id cannot be taken back")
        return value


async def test_a_restore_that_fails_does_not_stop_the_others(
    mongo_rs_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    async with beanie_database(mongo_rs_url, [RtDoc, RtGuarded]) as database:
        template = TransactionTemplate(MongoTransactionManager.for_client(database.client))
        guarded = RtGuarded(code="g")
        plain = RtDoc(code="p")
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="rolled back"):
            async with template.transaction():
                await MongoRepository(RtGuarded).save(guarded)
                await RtRepository().save(plain)
                raise RuntimeError("rolled back")
        _assert_new(plain)
        assert guarded.id is not None  # its restore failed, and was logged
        assert "an id cannot be taken back" in caplog.text


async def test_without_a_transaction_the_documents_written_before_a_failure_stay_saved(mongo_url: str) -> None:
    """On a standalone server a bulk write runs without a transaction and stops at the failing document: the ones
    before it are stored and keep their ids and revisions, the others get theirs back."""
    RtDoc.journal.clear()
    async with beanie_database(mongo_url, [RtDoc]) as db:
        repository = RtRepository()
        await repository.save(RtDoc(code="taken"))
        first, duplicate, last = RtDoc(code="first"), RtDoc(code="taken"), RtDoc(code="last")
        with pytest.raises(DuplicateKeyException):
            await repository.save_all([first, duplicate, last])
        await _assert_as_stored(first)
        _assert_new(duplicate, last)

        duplicate.code = "second"
        await repository.save_all([first, duplicate, last])
        await _assert_as_stored(first, duplicate, last)
        assert await _stored_codes(db) == {"taken": 0, "first": 0, "second": 0, "last": 0}
