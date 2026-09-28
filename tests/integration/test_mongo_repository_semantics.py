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
"""MongoRepository semantics on a real server: ids, field names, bulk saves, deletes, paging.

Each test binds its documents to a database of its own on the replica set and counts the commands the
client sent (:class:`~tests.support.mongo.CommandLog`), so a round trip too many shows.

- C038: string ids of ``ObjectId`` documents match in every ``_id`` filter; new ids are made on the client.
- C040: ``id`` is ``_id`` and an aliased field is stored under its alias in sorts, filters and derived
  queries; an unknown name is refused.
- C039: ``save_all`` is one bulk write that updates existing documents and runs Beanie's validation, event
  actions, revision checks and state management.
- C037: deletes and existence checks cost one command when nothing needs the documents themselves.
- WP03's port: ``delete_all_in_batch``, ``delete_all_by_id_in_batch``, ``find_slice``, NULL placement and
  case-insensitive orders, deterministic pages.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from beanie import Delete, Document, Insert, PydanticObjectId, Save, Update, ValidateOnSave, after_event, before_event
from bson import ObjectId
from pydantic import Field, field_validator

from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.post_processor import MongoRepositoryBeanPostProcessor
from pyfly.data.document.mongodb.repository import IN_CHUNK, MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.pageable import Order, Pageable, Sort
from pyfly.data.property_resolver import InvalidPropertyError
from pyfly.data.query_parser import InvalidQueryMethodError
from pyfly.data.transaction import TransactionTemplate
from pyfly.kernel.exceptions import DuplicateKeyException, OptimisticLockingFailureException
from tests.support.mongo import BeanieDatabase, beanie_database


class SemNote(BaseDocument):
    title: str
    status: str | None = None
    score: int | None = None
    display: str = Field(default="", alias="displayName")

    class Settings:
        name = "sem_notes"
        use_state_management = True


class SemTag(Document):
    id: str | None = None  # type: ignore[assignment]
    label: str

    class Settings:
        name = "sem_tags"


class SemTicket(Document):
    id: uuid.UUID | None = None  # type: ignore[assignment]
    subject: str

    class Settings:
        name = "sem_tickets"


class SemRevised(Document):
    name: str
    counter: int = 0

    class Settings:
        name = "sem_revised"
        use_revision = True


class SemValidated(Document):
    amount: int

    @field_validator("amount")
    @classmethod
    def _not_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("amount must not be negative")
        return value

    class Settings:
        name = "sem_validated"
        validate_on_save = True


class SemHooked(Document):
    name: str
    slug: str = ""
    deleted: ClassVar[list[str]] = []

    @before_event(Insert)
    def make_slug(self) -> None:
        self.slug = self.name.lower().replace(" ", "-")

    @before_event(Delete)
    def remember(self) -> None:
        SemHooked.deleted.append(self.name)

    class Settings:
        name = "sem_hooked"


class SemJournaled(Document):
    """Every event action ``save_all`` runs, recorded in order."""

    name: str
    journal: ClassVar[list[str]] = []

    @before_event(ValidateOnSave)
    def validating(self) -> None:
        SemJournaled.journal.append(f"validate {self.name}")

    @before_event(Insert)
    def inserting(self) -> None:
        SemJournaled.journal.append(f"before insert {self.name}")

    @after_event(Insert)
    async def inserted(self) -> None:
        SemJournaled.journal.append(f"after insert {self.name}")

    @after_event(Insert)
    async def tallied(self) -> None:
        SemJournaled.journal.append(f"tallied {self.name}")

    @before_event(Save)
    def saving(self) -> None:
        SemJournaled.journal.append(f"before save {self.name}")

    @before_event(Update)
    async def updating(self) -> None:
        SemJournaled.journal.append(f"before update {self.name}")

    @after_event(Update)
    def updated(self) -> None:
        SemJournaled.journal.append(f"after update {self.name}")

    @after_event(Save)
    async def saved(self) -> None:
        SemJournaled.journal.append(f"after save {self.name}")

    class Settings:
        name = "sem_journaled"


class NoteRepository(MongoRepository[SemNote, str]):
    async def find_by_status(self, status: str) -> list[SemNote]: ...

    async def delete_by_status(self, status: str) -> int: ...

    async def exists_by_status(self, status: str) -> bool: ...

    async def find_by_id_in(self, ids: list[str]) -> list[SemNote]: ...

    async def find_by_display(self, display: str) -> list[SemNote]: ...


MODELS = [SemNote, SemTag, SemTicket, SemRevised, SemValidated, SemHooked, SemJournaled]


@pytest.fixture
async def db(mongo_rs_url: str) -> AsyncIterator[BeanieDatabase]:
    SemHooked.deleted.clear()
    async with beanie_database(mongo_rs_url, MODELS) as database:
        yield database


def _notes() -> NoteRepository:
    repository = NoteRepository()
    MongoRepositoryBeanPostProcessor().after_init(repository, "noteRepository")
    return repository


async def _raw(db: BeanieDatabase, collection: str, **filter_: Any) -> list[dict[str, Any]]:
    return await db.database[collection].find(filter_).to_list()


# ---------------------------------------------------------------------------------------------------------
# C038: ids
# ---------------------------------------------------------------------------------------------------------


async def test_find_all_by_id_takes_the_string_ids_of_object_id_documents(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    saved = await repository.save_all([SemNote(title="a"), SemNote(title="b")])
    ids = [str(note.id) for note in saved]
    assert sorted(note.title for note in await repository.find_all_by_id(ids)) == ["a", "b"]


async def test_delete_all_by_id_and_exists_take_string_ids(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    first, second, third = await repository.save_all([SemNote(title=title) for title in "xyz"])
    assert await repository.exists_by_id(str(first.id)) is True
    await repository.delete_all_by_id([str(first.id), str(second.id)])
    await repository.delete_by_id(str(third.id))
    assert await repository.count() == 0


async def test_a_string_id_document_gets_a_string_id_and_is_found_by_it(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemTag, str] = MongoRepository(SemTag)
    tag = await repository.save(SemTag(label="python"))
    assert isinstance(tag.id, str) and ObjectId.is_valid(tag.id)
    stored = await _raw(db, "sem_tags")
    assert stored[0]["_id"] == tag.id  # stored as the string the document holds, not as an ObjectId
    found = await repository.find_by_id(tag.id)
    assert found is not None and found.label == "python"
    many = await repository.save_all([SemTag(label="a"), SemTag(label="b")])
    assert all(isinstance(item.id, str) for item in many)
    assert len(await repository.find_all_by_id([item.id for item in many])) == 2


async def test_a_uuid_id_document_gets_a_uuid(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemTicket, uuid.UUID] = MongoRepository(SemTicket)
    ticket = await repository.save(SemTicket(subject="broken"))
    assert isinstance(ticket.id, uuid.UUID)
    assert await repository.find_by_id(ticket.id) is not None
    assert await repository.exists_by_id(str(ticket.id)) is True


async def test_an_id_the_type_cannot_hold_matches_nothing(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save(SemNote(title="kept"))
    assert await repository.find_by_id("not-an-object-id") is None
    assert await repository.exists_by_id("not-an-object-id") is False
    assert await repository.find_all_by_id(["not-an-object-id"]) == []
    await repository.delete_by_id("not-an-object-id")
    await repository.delete_all_by_id(["not-an-object-id"])
    assert await repository.count() == 1


# ---------------------------------------------------------------------------------------------------------
# C040: field names
# ---------------------------------------------------------------------------------------------------------


async def test_sort_by_id_sorts_by_the_stored_id(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save_all([SemNote(title=title) for title in ("first", "second", "third")])
    newest_first = await repository.find_all(Sort.by(Order.desc("id")))
    assert [note.title for note in newest_first] == ["third", "second", "first"]


async def test_filters_and_sorts_by_an_aliased_field(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save_all(
        [SemNote(title="1", displayName="Zed"), SemNote(title="2", displayName="Amy"), SemNote(title="3")]
    )
    assert [note.title for note in await repository.find_all(display="Amy")] == ["2"]
    ordered = await repository.find_all(Sort.by(Order.desc("display")))
    assert [note.display for note in ordered] == ["Zed", "Amy", ""]
    assert (await _raw(db, "sem_notes", displayName="Zed"))[0]["title"] == "1"
    assert [note.title for note in await _notes().find_by_display("Zed")] == ["1"]


async def test_an_unknown_filter_or_sort_name_is_refused(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save(SemNote(title="x"))
    with pytest.raises(InvalidPropertyError):
        await repository.find_all(titel="x")
    with pytest.raises(InvalidPropertyError):
        await repository.find_all(Sort.by("titel"))
    with pytest.raises(InvalidPropertyError):
        await repository.find_all(**{"$where": "true"})


async def test_a_derived_query_on_an_unknown_field_fails_at_startup_instead_of_deleting_everything(
    db: BeanieDatabase,
) -> None:
    class Careless(MongoRepository[SemNote, str]):
        async def delete_by_nickname_is_null(self) -> int: ...

    await MongoRepository(SemNote).save_all([SemNote(title="a"), SemNote(title="b")])
    with pytest.raises(InvalidQueryMethodError, match="nickname"):
        MongoRepositoryBeanPostProcessor().after_init(Careless(), "careless")
    assert await MongoRepository(SemNote).count() == 2


async def test_sortable_narrows_the_callers_sort_but_not_a_derived_order_by(db: BeanieDatabase) -> None:
    class Narrowed(MongoRepository[SemNote, str]):
        __sortable__ = ("title",)

        async def find_by_status_order_by_score_desc(self, status: str) -> list[SemNote]: ...

        async def find_by_status(self, status: str, sort: Sort) -> list[SemNote]: ...

    repository = Narrowed()
    MongoRepositoryBeanPostProcessor().after_init(repository, "narrowed")
    await repository.save_all([SemNote(title=t, status="s", score=n) for t, n in (("a", 1), ("b", 3), ("c", 2))])
    assert [note.title for note in await repository.find_by_status_order_by_score_desc("s")] == ["b", "c", "a"]
    assert [note.title for note in await repository.find_by_status("s", Sort.by(Order.desc("title")))] == [
        "c",
        "b",
        "a",
    ]
    with pytest.raises(InvalidPropertyError):
        await repository.find_by_status("s", Sort.by("score"))
    with pytest.raises(InvalidPropertyError):
        await repository.find_all(Sort.by("score"))


async def test_a_derived_query_by_id_converts_the_id(db: BeanieDatabase) -> None:
    repository = _notes()
    saved = await repository.save_all([SemNote(title="by-id"), SemNote(title="other")])
    found = await repository.find_by_id_in([str(saved[0].id), "not-an-id"])
    assert [note.title for note in found] == ["by-id"]


class TitledNotes(MongoRepository[SemNote, str]):
    """A subclass written against the ``_query`` helper of earlier releases."""

    async def titled(self, display: str) -> list[SemNote]:
        return list(await self._query(display=display).to_list())


async def test_the_query_helper_of_earlier_releases_maps_names_and_runs_on_the_call_session(
    db: BeanieDatabase,
) -> None:
    repository = TitledNotes()
    await repository.save_all([SemNote(title="a", displayName="A"), SemNote(title="b", displayName="B")])
    db.log.clear()
    async with TransactionTemplate(MongoTransactionManager.for_client(db.client)).transaction():
        assert [note.title for note in await repository.titled("A")] == ["a"]
    (find,) = [body for name, body in db.log.commands if name == "find"]
    assert find["filter"] == {"displayName": "A"} and "txnNumber" in find
    with pytest.raises(InvalidPropertyError):
        await repository._query(nope=1).to_list()


# ---------------------------------------------------------------------------------------------------------
# C039: save_all
# ---------------------------------------------------------------------------------------------------------


async def test_save_all_updates_documents_that_exist(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    notes = await repository.save_all([SemNote(title="a", score=1), SemNote(title="b", score=2)])
    for note in notes:
        note.score = (note.score or 0) * 10
    await repository.save_all(notes)
    assert sorted((row["title"], row["score"]) for row in await _raw(db, "sem_notes")) == [("a", 10), ("b", 20)]


async def test_save_all_returns_documents_save_changes_can_write(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    (note,) = await repository.save_all([SemNote(title="tracked")])
    note.status = "done"
    await note.save_changes()
    assert (await _raw(db, "sem_notes"))[0]["status"] == "done"


async def test_save_all_validates_and_runs_insert_actions(db: BeanieDatabase) -> None:
    hooked: MongoRepository[SemHooked, str] = MongoRepository(SemHooked)
    await hooked.save_all([SemHooked(name="Hello World")])
    assert (await _raw(db, "sem_hooked"))[0]["slug"] == "hello-world"
    validated: MongoRepository[SemValidated, str] = MongoRepository(SemValidated)
    invalid = SemValidated(amount=1)
    invalid.amount = -5
    with pytest.raises(ValueError, match="negative"):
        await validated.save_all([invalid])
    assert await _raw(db, "sem_validated") == []


async def test_save_all_runs_every_event_action_of_its_documents_in_order(db: BeanieDatabase) -> None:
    SemJournaled.journal.clear()
    repository: MongoRepository[SemJournaled, str] = MongoRepository(SemJournaled)
    first, second = await repository.save_all([SemJournaled(name="a"), SemJournaled(name="b")])
    assert SemJournaled.journal == [
        "validate a",
        "before insert a",
        "validate b",
        "before insert b",
        "after insert a",
        "tallied a",
        "after insert b",
        "tallied b",
    ]
    SemJournaled.journal.clear()
    first.name = "c"
    await repository.save_all([first, SemJournaled(name="d")])
    assert SemJournaled.journal == [
        "validate c",
        "before save c",
        "before update c",
        "validate d",
        "before insert d",
        "after update c",
        "after save c",
        "after insert d",
        "tallied d",
    ]
    assert sorted(row["name"] for row in await _raw(db, "sem_journaled")) == ["b", "c", "d"]
    assert second.id is not None


async def test_save_all_is_one_bulk_write_for_new_and_existing_documents(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    existing = await repository.save(SemNote(title="old"))
    existing.status = "changed"
    db.log.clear()
    saved = await repository.save_all([existing, SemNote(title="new-1"), SemNote(title="new-2")])
    writes = [name for name in db.log.names() if name in ("insert", "update")]
    assert len(writes) <= 2 and "find" not in db.log.names()  # one ordered bulk write: an insert and an update batch
    assert all(note.id is not None for note in saved)
    assert sorted(row["title"] for row in await _raw(db, "sem_notes")) == ["new-1", "new-2", "old"]


async def test_save_all_refuses_a_stale_revision(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemRevised, str] = MongoRepository(SemRevised)
    (current,) = await repository.save_all([SemRevised(name="r")])
    stale = await repository.find_by_id(current.id)
    assert stale is not None
    current.counter = 1
    await repository.save_all([current])
    stale.counter = 99
    with pytest.raises(OptimisticLockingFailureException):
        await repository.save_all([stale])
    assert (await _raw(db, "sem_revised"))[0]["counter"] == 1


async def test_save_all_is_atomic_outside_a_transaction(db: BeanieDatabase) -> None:
    """A write auto unit on a replica set is a transaction: a duplicate in the batch writes nothing."""
    await db.database["sem_notes"].create_index("title", unique=True)
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    with pytest.raises(DuplicateKeyException):
        await repository.save_all([SemNote(title="one"), SemNote(title="two"), SemNote(title="one")])
    assert await _raw(db, "sem_notes") == []


# ---------------------------------------------------------------------------------------------------------
# C037: round trips
# ---------------------------------------------------------------------------------------------------------


async def test_a_derived_delete_is_one_delete_command(db: BeanieDatabase) -> None:
    repository = _notes()
    await MongoRepository(SemNote).save_all([SemNote(title=str(index), status="old") for index in range(5)])
    db.log.clear()
    assert await repository.delete_by_status("old") == 5
    assert db.log.names() == ["delete"]


async def test_delete_all_of_entities_and_delete_by_id_are_one_command_each(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    notes = await repository.save_all([SemNote(title=str(index)) for index in range(4)])
    db.log.clear()
    await repository.delete_all(notes[:3])
    assert db.log.names() == ["delete"]
    db.log.clear()
    await repository.delete_by_id(notes[3].id)
    assert db.log.names() == ["delete"]
    assert await repository.count() == 0


async def test_exists_reads_only_the_id_of_one_document(db: BeanieDatabase) -> None:
    repository = _notes()
    saved = await repository.save(SemNote(title="there", status="open"))
    db.log.clear()
    assert await repository.exists_by_id(saved.id) is True
    assert await repository.exists_by_status("open") is True
    assert await repository.exists_by_status("closed") is False
    assert db.log.names() == ["find", "find", "find"]
    for _name, body in db.log.commands:
        assert body.get("projection") == {"_id": 1}
        assert body.get("limit") == 1


@pytest.mark.parametrize("method", ["delete_all_by_id", "delete_all_by_id_in_batch"])
async def test_a_bulk_delete_of_more_ids_than_one_filter_holds_is_one_transaction(
    db: BeanieDatabase, method: str
) -> None:
    """A bulk delete sends one ``delete`` per :data:`IN_CHUNK` ids: outside a transaction, a list that needs more
    than one runs in a transaction of its own (atomic), and so does an iterator, which cannot be counted up front;
    a list that fits one filter is one command, with no transaction."""
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    kept = await repository.save(SemNote(title="kept"))
    delete = getattr(repository, method)
    ids = [str(ObjectId()) for _ in range(IN_CHUNK + 1)]

    def deletes() -> list[dict[str, Any]]:
        return [body for name, body in db.log.commands if name == "delete"]

    db.log.clear()
    await delete(ids)
    assert len(deletes()) == 2 and all(body.get("autocommit") is False for body in deletes())
    assert db.log.names()[-1] == "commitTransaction"
    db.log.clear()
    await delete(ids[:IN_CHUNK])
    assert db.log.names() == ["delete"] and "autocommit" not in deletes()[0]
    db.log.clear()
    await delete(iter([*ids[:2], str(kept.id)]))
    assert [body.get("autocommit") for body in deletes()] == [False]
    assert db.log.names()[-1] == "commitTransaction"
    assert await repository.count() == 0


async def test_delete_actions_still_run_document_by_document(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemHooked, str] = MongoRepository(SemHooked)
    hooked = await repository.save_all([SemHooked(name="one"), SemHooked(name="two"), SemHooked(name="three")])
    await repository.delete_all_by_id([hooked[0].id])
    await repository.delete_all([hooked[1]])
    assert SemHooked.deleted == ["one", "two"]
    await repository.delete_all_in_batch()  # in bulk: the actions are bypassed on purpose
    assert SemHooked.deleted == ["one", "two"]
    assert await repository.count() == 0


# ---------------------------------------------------------------------------------------------------------
# WP03's port on MongoDB
# ---------------------------------------------------------------------------------------------------------


async def test_batch_deletes_and_slices(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    notes = await repository.save_all([SemNote(title=f"n{index}", score=index) for index in range(7)])
    db.log.clear()
    first = await repository.find_slice(Pageable.of(1, 3, Sort.by("score")))
    assert [note.score for note in first.items] == [0, 1, 2] and first.has_next is True
    last = await repository.find_slice(Pageable.of(3, 3, Sort.by("score")))
    assert [note.score for note in last.items] == [6] and last.has_next is False
    assert "count" not in db.log.names() and "aggregate" not in db.log.names()
    await repository.delete_all_by_id_in_batch([str(notes[0].id), str(notes[1].id)])
    await repository.delete_all_in_batch(notes[2:4])
    assert await repository.count() == 3


async def test_null_handling_and_case_insensitive_orders(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save_all(
        [
            SemNote(title="b", score=2),
            SemNote(title="A", score=None),
            SemNote(title="c", score=1),
            SemNote(title="D"),
        ]
    )
    native = await repository.find_all(Sort.by(Order.asc("score")))
    assert [note.score for note in native][:2] == [None, None]  # MongoDB puts nulls first ascending
    nulls_last = await repository.find_all(Sort.by(Order.asc("score").nulls_last(), Order.asc("title")))
    assert [note.title for note in nulls_last] == ["c", "b", "A", "D"]
    nulls_first = await repository.find_all(Sort.by(Order.desc("score").nulls_first(), Order.asc("title")))
    assert [note.title for note in nulls_first] == ["A", "D", "b", "c"]
    by_case = await repository.find_all(Sort.by(Order.asc("title").ignoring_case()))
    assert [note.title for note in by_case] == ["A", "b", "c", "D"]
    page = await repository.find_all(Pageable.of(2, 2, Sort.by(Order.asc("title").ignoring_case())))
    assert [note.title for note in page.items] == ["c", "D"] and page.total == 4


async def test_pages_are_ordered_by_id_after_the_requested_orders(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    saved = await repository.save_all([SemNote(title=f"same-{index}", score=1) for index in range(6)])
    seen: list[PydanticObjectId | None] = []
    for page_number in (1, 2, 3):
        page = await repository.find_all(Pageable.of(page_number, 2, Sort.by("score")))
        seen.extend(note.id for note in page.items)
    assert seen == [note.id for note in saved]


async def test_stream_all_runs_on_its_own_unit_until_exhausted(db: BeanieDatabase) -> None:
    repository: MongoRepository[SemNote, str] = MongoRepository(SemNote)
    await repository.save_all([SemNote(title=f"s{index:02d}", score=index) for index in range(25)])
    streamed = [note.score async for note in repository.stream_all(Sort.by(Order.desc("score")))]
    assert streamed == list(range(24, -1, -1))
    filtered = [note.title async for note in repository.stream_all(Sort.by("title"), score=3)]
    assert filtered == ["s03"]
