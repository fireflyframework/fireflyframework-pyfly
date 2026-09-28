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

from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from beanie import Document, Insert, Save, after_event
from pymongo import IndexModel

from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.kernel.exceptions import ConcurrencyException, DuplicateKeyException, OptimisticLockingFailureException
from tests.support.mongo import BeanieDatabase, beanie_database


class RtDoc(Document):
    code: str
    n: int = 0
    journal: ClassVar[list[str]] = []

    class Settings:
        name = "rt_docs"
        use_revision = True
        use_state_management = True
        indexes = [IndexModel("code", unique=True)]

    @after_event(Insert)
    def inserted(self) -> None:
        RtDoc.journal.append(f"insert {self.code}")

    @after_event(Save)
    def saved(self) -> None:
        RtDoc.journal.append(f"save {self.code}")


class RtRepository(MongoRepository[RtDoc, str]):
    async def import_then_fail(self, documents: list[RtDoc]) -> None:
        """Saves *documents*, then fails: outside a transaction the method's own unit rolls the save back."""
        await self.save_all(documents)
        raise RuntimeError("after the save")


@pytest.fixture
async def db(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    RtDoc.journal.clear()
    async with beanie_database(mongo_rs_url, [RtDoc]) as database:
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
