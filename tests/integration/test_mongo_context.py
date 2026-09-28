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
"""The document layer in a running ApplicationContext, on a real MongoDB replica set.

- C106: the client is built from ``pyfly.data.document.*`` (pool, timeouts, ``tz_aware``, options), and
  ``BaseDocument`` timestamps come back as aware UTC values.
- C107: ``BaseDocument`` audit fields follow the application's ``AuditorAware`` and ``DateTimeProvider`` on
  every write path, ``run_as`` included.
- C035: documents that are not ``BaseDocument`` subclasses, linked documents and configured model packages are
  initialized.
- C108: two contexts in one process share the binding and the client, and a context that would rebind the
  documents to another database fails fast.
- WP06-01 wiring: the context registers the MongoDB transaction manager, so ``@transactional`` finds it by
  datasource, by ``_motor_client`` and as the default datasource.
- C166: the readiness indicator and the command and pool metrics.
- A MongoDB repository whose query stubs nothing compiles fails the start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import Document, Link
from prometheus_client import REGISTRY
from pymongo import AsyncMongoClient

from pyfly.container import bean, configuration
from pyfly.container.exceptions import BeanCreationException
from pyfly.context.application_context import ApplicationContext
from pyfly.data.auditing import AuditorAware, DateTimeProvider, run_as
from pyfly.data.document.mongodb.document import BaseDocument
from pyfly.data.document.mongodb.health import MongoHealthIndicator
from pyfly.data.document.mongodb.initializer import BINDINGS, DocumentBindingError
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.transaction import TransactionManagerRegistry, is_transaction_active, transactional
from tests.support.backend_matrix import MongoBackend


class CtxNote(BaseDocument):
    text: str
    stamped_at: datetime | None = None

    class Settings:
        name = "ctx_notes"
        use_state_management = True


class CtxAuthor(Document):
    name: str

    class Settings:
        name = "ctx_authors"


class CtxBook(Document):
    title: str
    author: Link[CtxAuthor] | None = None

    class Settings:
        name = "ctx_books"


class CtxNoteRepository(MongoRepository[CtxNote, str]):
    async def find_by_text(self, text: str) -> list[CtxNote]: ...


class CtxBookRepository(MongoRepository[CtxBook, str]):
    pass


class FixedClock(DateTimeProvider):
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)

    def get_now(self) -> datetime:
        return self.now


class FixedAuditor(AuditorAware):
    user: str | None = "alice"

    async def get_current_auditor(self) -> str | None:
        return FixedAuditor.user


CLOCK = FixedClock()


@configuration
class AuditingPorts:
    @bean
    def auditor(self) -> AuditorAware:
        return FixedAuditor()

    @bean
    def clock(self) -> DateTimeProvider:
        return CLOCK


class NoteService:
    def __init__(self, notes: CtxNoteRepository) -> None:
        self.notes = notes

    @transactional(datasource="document")
    async def write_then_fail(self, text: str) -> None:
        await self.notes.save(CtxNote(text=text))
        raise ValueError("rolled back")

    @transactional
    async def on_the_default_datasource(self, text: str) -> bool:
        await self.notes.save(CtxNote(text=text))
        return is_transaction_active("document")


class LegacyNoteService:
    def __init__(self, notes: CtxNoteRepository, client: AsyncMongoClient[Any]) -> None:
        self.notes = notes
        self._motor_client = client

    @transactional
    async def write_then_fail(self, text: str, *, session: Any = None) -> Any:
        await self.notes.save(CtxNote(text=text))
        await CtxNote(text=f"{text}-raw").insert(session=session)
        raise ValueError("rolled back")


async def _start(backend: MongoBackend, *beans: type, overrides: dict[str, Any] | None = None) -> ApplicationContext:
    context = ApplicationContext(backend.config(overrides))
    for bean_class in beans:
        context.register_bean(bean_class)
    await context.start()
    return context


@pytest.fixture
async def context(mongo_backend: MongoBackend) -> AsyncIterator[ApplicationContext]:
    FixedAuditor.user = "alice"
    CLOCK.now = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    ctx = await _start(
        mongo_backend,
        CtxNoteRepository,
        CtxBookRepository,
        NoteService,
        AuditingPorts,
        overrides={"pyfly.data.document.max-pool-size": "7", "pyfly.data.document.app-name": "wp06-it"},
    )
    try:
        yield ctx
    finally:
        await ctx.stop()


async def _texts(backend: MongoBackend) -> list[str]:
    reader: AsyncMongoClient[Any] = AsyncMongoClient(backend.url)
    try:
        rows = await reader[backend.database]["ctx_notes"].find({}).sort("text", 1).to_list()
        return [row["text"] for row in rows]
    finally:
        await reader.close()


# ---------------------------------------------------------------------------------------------------------
# C106: the client
# ---------------------------------------------------------------------------------------------------------


async def test_the_client_is_built_from_the_properties(context: ApplicationContext) -> None:
    client = context.get_bean(AsyncMongoClient)
    options = client.options
    assert options.pool_options.max_pool_size == 7
    assert options.pool_options.metadata["application"]["name"] == "wp06-it"
    assert options.codec_options.tz_aware is True
    assert options.codec_options.uuid_representation == 4  # standard


async def test_base_document_timestamps_come_back_as_aware_utc(context: ApplicationContext) -> None:
    notes = context.get_bean(CtxNoteRepository)
    saved = await notes.save(CtxNote(text="tz"))
    loaded = await notes.find_by_id(saved.id)
    assert loaded is not None
    assert loaded.created_at.tzinfo is not None and loaded.created_at.utcoffset().total_seconds() == 0
    assert loaded.created_at == saved.created_at  # stamped to the millisecond BSON keeps


# ---------------------------------------------------------------------------------------------------------
# C107: auditing
# ---------------------------------------------------------------------------------------------------------


async def test_audit_fields_follow_the_auditor_and_the_clock(context: ApplicationContext) -> None:
    notes = context.get_bean(CtxNoteRepository)
    clock = CLOCK
    created = await notes.save(CtxNote(text="audited"))
    assert created.created_by == "alice" and created.updated_by == "alice"
    assert created.created_at == datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)

    clock.now = datetime(2026, 2, 1, tzinfo=UTC)
    FixedAuditor.user = "bob"
    loaded = await notes.find_by_id(created.id)
    assert loaded is not None
    loaded.text = "edited"
    await notes.save(loaded)
    reloaded = await notes.find_by_id(created.id)
    assert reloaded is not None
    assert (reloaded.created_by, reloaded.updated_by) == ("alice", "bob")
    assert reloaded.created_at == datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    assert reloaded.updated_at == datetime(2026, 2, 1, tzinfo=UTC)

    clock.now = datetime(2026, 3, 1, tzinfo=UTC)
    FixedAuditor.user = None
    reloaded.text = "by a job with no principal"
    await reloaded.save_changes()
    again = await notes.find_by_id(created.id)
    assert again is not None and again.updated_by is None and again.updated_at == datetime(2026, 3, 1, tzinfo=UTC)

    clock.now = datetime(2026, 4, 1, tzinfo=UTC)
    await again.set({CtxNote.text: "set directly"})
    final = await notes.find_by_id(created.id)
    assert final is not None and final.updated_at == datetime(2026, 4, 1, tzinfo=UTC)


async def test_bulk_saves_are_stamped(context: ApplicationContext) -> None:
    notes = context.get_bean(CtxNoteRepository)
    batch = await notes.save_all([CtxNote(text="a"), CtxNote(text="b")])
    assert [note.created_by for note in batch] == ["alice", "alice"]
    FixedAuditor.user = "carol"
    for note in batch:
        note.text += "!"
    await notes.save_all(batch)
    stored = await notes.find_all()
    assert sorted((note.text, note.created_by, note.updated_by) for note in stored) == [
        ("a!", "alice", "carol"),
        ("b!", "alice", "carol"),
    ]


async def test_run_as_names_the_principal_of_the_default_auditor(mongo_backend: MongoBackend) -> None:
    ctx = await _start(mongo_backend, CtxNoteRepository)
    try:
        notes = ctx.get_bean(CtxNoteRepository)
        with run_as("nightly-job"):
            saved = await notes.save(CtxNote(text="job"))
        assert (saved.created_by, saved.updated_by) == ("nightly-job", "nightly-job")
    finally:
        await ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# C035: discovery
# ---------------------------------------------------------------------------------------------------------


async def test_plain_documents_and_linked_documents_are_initialized(context: ApplicationContext) -> None:
    books = context.get_bean(CtxBookRepository)
    author = CtxAuthor(name="Ursula")
    await author.insert()  # CtxAuthor has no repository: it is initialized as the target of CtxBook's link
    book = await books.save(CtxBook(title="The Dispossessed", author=author))  # type: ignore[arg-type]
    found = await books.find_by_id(book.id)
    assert found is not None and found.title == "The Dispossessed"
    assert await books.count() == 1


# ---------------------------------------------------------------------------------------------------------
# C108: one binding per process
# ---------------------------------------------------------------------------------------------------------


async def test_two_contexts_share_the_binding_and_the_client(mongo_backend: MongoBackend) -> None:
    first = await _start(mongo_backend, CtxNoteRepository)
    second = ApplicationContext(mongo_backend.config())
    second.register_bean(CtxNoteRepository)
    try:
        await second.start()
        client = first.get_bean(AsyncMongoClient)
        assert second.get_bean(AsyncMongoClient) is client
        assert BINDINGS.users(client) == 2
        await first.stop()
        # The second context keeps its documents and its client.
        notes = second.get_bean(CtxNoteRepository)
        await notes.save(CtxNote(text="still bound"))
        assert [note.text for note in await notes.find_all()] == ["still bound"]
    finally:
        await first.stop()
        await second.stop()
    assert BINDINGS.users(client) == 0


async def test_a_context_that_would_rebind_the_documents_fails_fast(mongo_backend: MongoBackend) -> None:
    first = await _start(mongo_backend, CtxNoteRepository)
    try:
        other = MongoBackend(url=mongo_backend.url, database=f"{mongo_backend.database}_other")
        with pytest.raises(BeanCreationException) as refused:
            await _start(other, CtxNoteRepository)
        assert isinstance(refused.value.__cause__, DocumentBindingError) or "bound to database" in str(refused.value)
        notes = first.get_bean(CtxNoteRepository)
        await notes.save(CtxNote(text="first keeps its database"))
        assert await _texts(mongo_backend) == ["first keeps its database"]
    finally:
        await first.stop()
    # Once the first context stopped, another database is fine.
    other = MongoBackend(url=mongo_backend.url, database=f"{mongo_backend.database}_next")
    third = await _start(other, CtxNoteRepository)
    try:
        await third.get_bean(CtxNoteRepository).save(CtxNote(text="rebound"))
    finally:
        await third.stop()
        cleaner: AsyncMongoClient[Any] = AsyncMongoClient(mongo_backend.url)
        await cleaner.drop_database(other.database)
        await cleaner.close()


# ---------------------------------------------------------------------------------------------------------
# WP06-01: the transaction manager in the context
# ---------------------------------------------------------------------------------------------------------


async def test_the_context_registers_the_document_transaction_manager(
    context: ApplicationContext, mongo_backend: MongoBackend
) -> None:
    registry = context.get_bean(TransactionManagerRegistry)
    manager = context.get_bean(MongoTransactionManager)
    assert registry.get("document") is manager
    assert registry.default is manager  # no relational layer: the document datasource is the default
    service = context.get_bean(NoteService)
    with pytest.raises(ValueError):
        await service.write_then_fail("named")
    assert await service.on_the_default_datasource("default") is True
    assert await _texts(mongo_backend) == ["default"]


async def test_a_legacy_motor_client_service_runs_in_the_context_manager(
    context: ApplicationContext, mongo_backend: MongoBackend
) -> None:
    client = context.get_bean(AsyncMongoClient)
    service = LegacyNoteService(context.get_bean(CtxNoteRepository), client)
    with pytest.raises(ValueError):
        await service.write_then_fail("legacy")
    assert await _texts(mongo_backend) == []


# ---------------------------------------------------------------------------------------------------------
# C166: health and metrics
# ---------------------------------------------------------------------------------------------------------


async def test_the_health_indicator_answers_readiness(context: ApplicationContext) -> None:
    indicator = context.get_bean(MongoHealthIndicator)
    status = await indicator.health()
    assert status.status == "UP"
    assert status.details["datasource"] == "document" and status.details["transactions"] is True
    unreachable: AsyncMongoClient[Any] = AsyncMongoClient("mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=5000")
    try:
        down = await MongoHealthIndicator(unreachable, timeout=0.3).health()
    finally:
        await unreachable.close()
    assert down.status == "DOWN"


async def test_commands_and_the_pool_feed_the_metrics(context: ApplicationContext) -> None:
    def sample(name: str, **labels: str) -> float:
        return REGISTRY.get_sample_value(name, {"datasource": "document", **labels}) or 0.0

    before = sample("pyfly_mongo_commands_total", command="insert", outcome="success")
    await context.get_bean(CtxNoteRepository).save(CtxNote(text="counted"))
    assert sample("pyfly_mongo_commands_total", command="insert", outcome="success") == before + 1
    assert sample("pyfly_mongo_command_duration_seconds_count", command="insert") >= 1
    assert sample("pyfly_mongo_pool_open") >= 1


# ---------------------------------------------------------------------------------------------------------
# Wiring check
# ---------------------------------------------------------------------------------------------------------


async def test_uncompiled_query_stubs_fail_the_start(mongo_backend: MongoBackend) -> None:
    context = ApplicationContext(mongo_backend.config({"pyfly.data.document.enabled": "false"}))
    context.register_bean(CtxNoteRepository)
    with pytest.raises(BeanCreationException, match="find_by_text"):
        await context.start()
    await context.stop()
