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
"""Unified @transactional — ONE annotation for the relational and the document backend.

The relational arm runs on a real SQLite file database through the unit of work (a repository with no
session of its own joins the unit the service's legacy ``_session_factory`` resolves to). The document arm runs
on a real MongoDB replica set through the MongoDB transaction manager (a service's legacy ``_motor_client``
resolves to it). A service that exposes both must name its datasource (C143), and each datasource has units of
its own: a ``REQUIRED`` boundary on one datasource inside a unit of the other starts its own transaction, which
commits or rolls back on its own (there is no two-phase commit between them).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from beanie import Indexed
from pymongo import AsyncMongoClient
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.context.events import ApplicationEventBus, ApplicationEventPublisher
from pyfly.data import Isolation, transactional
from pyfly.data.document.mongodb import mongo_transactional
from pyfly.data.document.mongodb.document import AggregateDocument, BaseDocument
from pyfly.data.document.mongodb.repository import MongoRepository
from pyfly.data.document.mongodb.transaction_manager import MongoTransactionManager
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    TransactionManagerRegistry,
    current_unit_of_work,
    install_registry,
    uninstall_registry,
)
from pyfly.domain import DomainEvent
from pyfly.eda.domain_events import DomainEventPublisher
from pyfly.kernel.exceptions import DuplicateKeyException
from tests.support.mongo import beanie_database


def test_one_annotation_is_shared_across_backends() -> None:
    from pyfly.data.relational.sqlalchemy import transactional as relational_transactional

    assert transactional is relational_transactional
    assert mongo_transactional is transactional  # deprecated alias


# --------------------------------------------------------------------------- relational dispatch
class UnifiedRow(Base):
    __tablename__ = "unified_row"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


async def _relational_service(tmp_path: Path) -> tuple[Any, str, AsyncEngine]:
    """A service on a real SQLite file database, dispatched through its legacy ``_session_factory``."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'unified.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[UnifiedRow.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Svc:
        def __init__(self) -> None:
            self._session_factory = factory
            self.repo = Repository(UnifiedRow)

        @transactional()
        async def commit_path(self) -> str:
            await self.repo.save(UnifiedRow(name="committed"))
            return "ok"

        @transactional(isolation=Isolation.SERIALIZABLE)
        async def with_isolation(self) -> str:
            unit = current_unit_of_work()
            assert unit is not None
            level = await (await unit.resource.connection()).get_isolation_level()
            return str(level)

        @transactional()
        async def failing(self) -> str:
            await self.repo.save(UnifiedRow(name="rolled-back"))
            raise ValueError("boom")

    return Svc(), url, engine


async def _names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return [row[0] for row in (await conn.execute(text("SELECT name FROM unified_row ORDER BY id"))).all()]


@pytest.mark.asyncio
async def test_relational_commits_on_success(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        assert await svc.commit_path() == "ok"
        assert await _names(engine) == ["committed"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_isolation_reaches_the_connection(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        # The audit flagged this as unverified behind a mock (F6): read the level back from the connection.
        assert await svc.with_isolation() == "SERIALIZABLE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relational_rolls_back_on_exception(tmp_path: Path) -> None:
    svc, _url, engine = await _relational_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="boom"):
            await svc.failing()
        assert await _names(engine) == []
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_polyglot_service_must_name_its_datasource(tmp_path: Path) -> None:
    """C143: exposing both factories used to run only the relational arm, so Mongo writes committed."""
    svc, _url, engine = await _relational_service(tmp_path)
    svc._motor_client = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
    try:
        with pytest.raises(IllegalTransactionStateError, match="both"):
            await svc.commit_path()
        assert await _names(engine) == []
    finally:
        await engine.dispose()
        await svc._motor_client.close()


@pytest.mark.asyncio
async def test_no_manager_anywhere_raises() -> None:
    class NoClient:
        @transactional()
        async def work(self, *, session: Any = None) -> None: ...

    # Neither factory attribute and no running context: an IllegalTransactionStateError (a RuntimeError).
    with pytest.raises(RuntimeError, match="No transaction manager"):
        await NoClient().work()


@pytest.mark.asyncio
async def test_a_motor_client_that_is_not_pymongos_is_refused() -> None:
    class OtherClient:
        pass

    class Svc:
        def __init__(self) -> None:
            self._motor_client = OtherClient()

        @transactional()
        async def work(self) -> None: ...

    with pytest.raises(IllegalTransactionStateError, match="_motor_client"):
        await Svc().work()


# --------------------------------------------------------------------------- document dispatch (replica set)
class UnifiedDoc(BaseDocument):
    name: str

    class Settings:
        name = "unified_docs"


@dataclass(frozen=True)
class UnifiedRenamed(DomainEvent):
    name: str = ""


class UnifiedAggregate(AggregateDocument):
    name: Indexed(str, unique=True)  # type: ignore[valid-type]

    class Settings:
        name = "unified_aggregates"

    def rename(self, name: str) -> None:
        self.name = name
        self.raise_event(UnifiedRenamed(name=name))


@dataclass
class Polyglot:
    service: Any
    engine: AsyncEngine
    documents: Any


@pytest.fixture
async def polyglot(tmp_path: Path, mongo_rs_url: str) -> AsyncIterator[Polyglot]:
    """A relational SQLite file database and a MongoDB replica set, each with its own transaction manager."""
    service, _url, engine = await _relational_service(tmp_path)
    async with beanie_database(mongo_rs_url, [UnifiedDoc, UnifiedAggregate]) as documents:
        service._motor_client = documents.client
        service.docs = MongoRepository(UnifiedDoc)
        try:
            yield Polyglot(service, engine, documents)
        finally:
            await engine.dispose()


async def _doc_names(polyglot: Polyglot) -> list[str]:
    rows = await polyglot.documents.database["unified_docs"].find({}).sort("name", 1).to_list()
    return [row["name"] for row in rows]


@pytest.mark.asyncio
async def test_the_document_arm_commits_and_passes_its_session(polyglot: Polyglot) -> None:
    client = polyglot.documents.client

    class DocumentService:
        def __init__(self) -> None:
            self._motor_client = client
            self.docs = MongoRepository(UnifiedDoc)

        @transactional()
        async def commit_path(self, *, session: Any = None) -> Any:
            await self.docs.save(UnifiedDoc(name="repository"))
            await UnifiedDoc(name="raw").insert(session=session)
            return session

        @transactional()
        async def fails(self, *, session: Any = None) -> None:
            await self.docs.save(UnifiedDoc(name="rolled-back"))
            raise ValueError("boom")

        @transactional(no_rollback_for=(KeyError,))
        async def fails_no_rollback(self) -> None:
            await self.docs.save(UnifiedDoc(name="kept"))
            raise KeyError("ignored")

    service = DocumentService()
    session = await service.commit_path()
    assert session is not None and session.has_ended  # the unit's session, ended with the unit
    with pytest.raises(ValueError, match="boom"):
        await service.fails()
    with pytest.raises(KeyError):
        await service.fails_no_rollback()  # no_rollback_for: committed, and the exception still surfaces
    assert await _doc_names(polyglot) == ["kept", "raw", "repository"]


@pytest.mark.asyncio
async def test_a_polyglot_service_that_names_its_datasource_runs_there(polyglot: Polyglot) -> None:
    service = polyglot.service
    manager = MongoTransactionManager.for_client(polyglot.documents.client)

    class Named:
        _session_factory = service._session_factory
        _motor_client = polyglot.documents.client

        @transactional(datasource=manager.datasource)
        async def documents_only(self) -> None:
            await service.docs.save(UnifiedDoc(name="named"))
            await service.repo.save(UnifiedRow(name="relational, in a short unit of its own"))
            raise ValueError("rolled back on the document datasource")

    registry = TransactionManagerRegistry(default=manager.datasource)
    registry.register(manager)
    registry.register(SqlAlchemyTransactionManager.for_sessionmaker(service._session_factory))
    install_registry(registry)
    try:
        with pytest.raises(ValueError):
            await Named().documents_only()
    finally:
        uninstall_registry(registry)
    assert await _doc_names(polyglot) == []
    assert await _names(polyglot.engine) == ["relational, in a short unit of its own"]


@pytest.mark.asyncio
async def test_a_required_document_unit_inside_a_relational_unit_starts_its_own(polyglot: Polyglot) -> None:
    """A unit that belongs to another datasource never satisfies a join (C060): the document boundary begins
    its own transaction and commits at its own end, whatever the relational unit around it does next."""
    service = polyglot.service
    manager = MongoTransactionManager.for_client(polyglot.documents.client)
    seen: dict[str, Any] = {}

    @transactional(manager=manager)
    async def record(name: str) -> None:
        seen["relational"] = current_unit_of_work("primary")
        seen["document"] = current_unit_of_work(manager.datasource)
        await service.docs.save(UnifiedDoc(name=name))

    class Outer:
        def __init__(self) -> None:
            self._session_factory = service._session_factory
            self.repo = service.repo

        @transactional()
        async def write_both_then_fail(self) -> None:
            await self.repo.save(UnifiedRow(name="relational"))
            await record("document")
            raise ValueError("the relational unit rolls back")

    with pytest.raises(ValueError):
        await Outer().write_both_then_fail()
    assert await _names(polyglot.engine) == []  # the relational unit rolled back
    assert await _doc_names(polyglot) == ["document"]  # the document unit had committed on its own
    assert seen["relational"] is not None and seen["document"] is not None
    assert seen["relational"] is not seen["document"]


@pytest.mark.asyncio
async def test_an_aggregate_document_ties_its_events_to_a_document_unit_only(polyglot: Polyglot) -> None:
    """An event an aggregate document raises inside a relational unit, with no document unit around it, is not
    tied to the relational unit: it is published when a document unit saves the document. A failed save leaves it
    pending, and the relational unit's commit does not publish the event of a document that was never saved."""
    service = polyglot.service
    aggregates: MongoRepository[UnifiedAggregate, str] = MongoRepository(UnifiedAggregate)
    await aggregates.save(UnifiedAggregate(name="taken"))
    bus = ApplicationEventBus()
    seen: list[str] = []

    async def on_renamed(event: UnifiedRenamed) -> None:
        seen.append(event.name)

    bus.subscribe(UnifiedRenamed, on_renamed)
    publisher = DomainEventPublisher(ApplicationEventPublisher(bus))

    class Outer:
        _session_factory = service._session_factory

        @transactional()
        async def rename(self, aggregate: UnifiedAggregate, name: str) -> None:
            await service.repo.save(UnifiedRow(name=f"renamed to {name}"))
            aggregate.rename(name)
            with contextlib.suppress(DuplicateKeyException):
                await aggregates.save(aggregate)

    await publisher.start()
    try:
        clashing = UnifiedAggregate(name="new")
        await Outer().rename(clashing, "taken")  # the document save fails, the relational unit commits
        assert await _names(polyglot.engine) == ["renamed to taken"]
        assert seen == []
        assert [event.name for event in clashing.pending_events()] == ["taken"]
        saved = UnifiedAggregate(name="other")
        await Outer().rename(saved, "renamed")  # published once, as the document save commits
        assert seen == ["renamed"]
        assert saved.pending_events() == []
    finally:
        await publisher.stop()
