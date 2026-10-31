# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native repository writes and mandatory journal facts share one SQL transaction."""

from contextlib import asynccontextmanager

import pytest
from sqlalchemy import String
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pyfly.audit import (
    AuditActor,
    AuditContext,
    AuditContextResolver,
    AuditError,
    AuditJournal,
    AuditResource,
    AuditScope,
)
from pyfly.audit._infrastructure import audit_infrastructure
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.recording_auto_configuration import AuditRecordingAutoConfiguration
from pyfly.audit.sql import SqlAuditJournal
from pyfly.container import bean, configuration, repository
from pyfly.context import ApplicationContext
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import Repository, SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate, UnexpectedRollbackError
from pyfly.kernel.exceptions import DataIntegrityException
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")
SCOPE = AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project")


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "audit_repository_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(String(128))


@repository
class Items(Repository[Item, str]):
    async def delete_by_content(self, content: str) -> None: ...

    async def create_pair(self) -> None:
        await self.save(Item(id="first", content="nested first"))
        await self.save(Item(id="second", content="nested second"))


class TrustedResolver:
    def __init__(self):
        self.operations = []
        self.broken = False

    async def resolve(self, operation):
        self.operations.append(operation)
        if self.broken:
            raise ValueError("private resolver detail")
        return AuditContext(
            scope=SCOPE,
            actor=AuditActor(kind="service", subject_id="verified-host"),
            resource=AuditResource(kind="record", resource_id="authoritative-resource"),
        )


@asynccontextmanager
async def application(backend: RelationalBackend, *, modules=None, enabled=True, manual=False):
    engine = backend.create_engine()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    schema = SqlAuditJournal(engine)
    await schema.start()
    resolver = TrustedResolver()
    session = async_sessionmaker(engine, expire_on_commit=False)() if manual else None

    @configuration
    class Authority:
        @bean
        def trusted_audit_context(self) -> AuditContextResolver:
            return resolver

    context = ApplicationContext(
        backend.config(
            {
                "pyfly.audit.enabled": "true",
                "pyfly.audit.recording.enabled": "true" if enabled else "false",
                "pyfly.audit.recording.modules": modules if modules is not None else ["data"],
            }
        ),
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AuditRecordingAutoConfiguration,
        ],
    )
    context.register_bean(Authority)
    if manual:

        @configuration
        class ManualRepository:
            @bean
            def manual_items(self) -> Items:
                return Items(session=session)

        context.register_bean(ManualRepository)
    else:
        context.register_bean(Items)
    try:
        await context.start()
        yield context, resolver
    finally:
        await context.stop()
        if session is not None:
            await session.close()
        await schema.stop()


async def test_managed_auto_write_records_safe_admission_and_reads_do_not_recurse(relational_backend):
    async with application(relational_backend) as (context, resolver):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        await items.save(Item(id="one", content="private business content"))
        assert await items.count() == 1
        assert (await items.find_by_id("one")).content == "private business content"
        entries = await journal.read(SCOPE)
        assert len(entries) == len(resolver.operations) == 1
        entry = entries[0]
        assert entry.event.event_type == "data.repository.write"
        assert entry.event.outcome == "admitted"
        assert entry.event.actor.subject_id == "verified-host"
        assert entry.event.resource.resource_id == "authoritative-resource"
        assert "private business content" not in (await journal.export(SCOPE, through_sequence=1)).decode()
        assert len(await journal.outbox.pending("pyfly.audit.export")) == 1


async def test_business_rollback_removes_repository_fact_and_native_outbox(relational_backend):
    async with application(relational_backend) as (context, _):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        manager = context.get_bean(SqlAlchemyTransactionManager)
        with pytest.raises(RuntimeError, match="abort"):
            async with TransactionTemplate(manager).transaction():
                await items.save(Item(id="one", content="must roll back"))
                assert len(await journal.read(SCOPE)) == 1
                raise RuntimeError("abort")
        assert await items.count() == 0
        assert not await journal.read(SCOPE)
        assert not await journal.outbox.pending("pyfly.audit.export")


async def test_caught_mandatory_failure_prevents_unaudited_commit_and_hides_resolver_detail(relational_backend):
    async with application(relational_backend) as (context, resolver):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        manager = context.get_bean(SqlAlchemyTransactionManager)
        with pytest.raises(UnexpectedRollbackError) as rollback:
            async with TransactionTemplate(manager).transaction():
                await items.save(Item(id="first", content="must roll back"))
                resolver.broken = True
                with pytest.raises(AuditError) as failure:
                    await items.save(Item(id="second", content="never accepted"))
                assert "private resolver detail" not in str(failure.value)
        assert "private resolver detail" not in str(rollback.value)
        assert await items.count() == 0
        assert not await journal.read(SCOPE)
        assert not await journal.outbox.pending("pyfly.audit.export")


async def test_auto_unit_rolls_back_when_mandatory_recording_is_unavailable(relational_backend):
    async with application(relational_backend) as (context, resolver):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        resolver.broken = True
        with pytest.raises(AuditError):
            await items.save(Item(id="one", content="not accepted"))
        assert await items.count() == 0
        assert not await journal.read(SCOPE)


@pytest.mark.parametrize("mode", ["disabled", "other-module"])
async def test_non_enrolled_data_keeps_existing_repository_behavior(relational_backend, mode):
    async with application(
        relational_backend, enabled=mode != "disabled", modules=["web"] if mode == "other-module" else ["data"]
    ) as (context, resolver):
        await context.get_bean(Items).save(Item(id="one", content="ordinary data"))
        assert await context.get_bean(Items).count() == 1
        assert not resolver.operations
        assert not await context.get_bean(AuditJournal).read(SCOPE)


async def test_infrastructure_suppression_is_scoped_to_its_actual_journal(relational_backend):
    async with application(relational_backend) as (context, resolver):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        with audit_infrastructure(object()):
            await items.save(Item(id="business", content="must be captured"))
        with audit_infrastructure(journal):
            await items.save(Item(id="infrastructure", content="trusted internal write"))
        assert await items.count() == 2
        assert len(resolver.operations) == len(await journal.read(SCOPE)) == 1


async def test_backend_write_failure_does_not_commit_a_false_admission(relational_backend):
    async with application(relational_backend) as (context, _):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        await items.save(Item(id="one", content="original"))
        with pytest.raises(DataIntegrityException):
            await items.save(Item(id="invalid", content=None))
        assert await items.count() == 1
        assert len(await journal.read(SCOPE)) == 1
        assert len(await journal.outbox.pending("pyfly.audit.export")) == 1


async def test_enrolled_manual_session_cannot_bypass_the_native_atomic_write_contract(relational_backend):
    async with application(relational_backend, manual=True) as (context, _):
        items = context.get_bean(Items)
        with pytest.raises(AuditError, match="managed repository"):
            await items.save(Item(id="one", content="no hidden dual write"))
        assert await items.count() == 0
        assert not await context.get_bean(AuditJournal).read(SCOPE)


async def test_native_compiled_write_query_is_enrolled_without_capturing_its_arguments(relational_backend):
    async with application(relational_backend) as (context, resolver):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        await items.save(Item(id="one", content="private query argument"))
        await items.delete_by_content("private query argument")
        assert await items.count() == 0
        assert len(await journal.read(SCOPE)) == len(resolver.operations) == 2
        assert "private query argument" not in repr(resolver.operations)
        assert "private query argument" not in (await journal.export(SCOPE, through_sequence=2)).decode()


async def test_nested_managed_writes_share_native_unit_without_recursive_guard_lock(relational_backend):
    async with application(relational_backend) as (context, resolver):
        await context.get_bean(Items).create_pair()
        assert await context.get_bean(Items).count() == 2
        entries = await context.get_bean(AuditJournal).read(SCOPE)
        assert len(entries) == len(resolver.operations) == 3


async def test_native_outbox_failure_rolls_back_the_new_fact_and_business_write(relational_backend, monkeypatch):
    async with application(relational_backend) as (context, _):
        items, journal = context.get_bean(Items), context.get_bean(AuditJournal)
        await items.save(Item(id="original", content="preserved"))
        before = await journal.read(SCOPE)

        async def unavailable(*args, **kwargs):
            raise RuntimeError("private outbox failure detail")

        monkeypatch.setattr(journal.outbox, "append", unavailable)
        with pytest.raises(AuditError) as failed:
            await items.save(Item(id="new", content="never accepted"))
        assert "private outbox failure detail" not in str(failed.value)
        assert await items.count() == 1
        assert (await items.find_by_id("original")).content == "preserved"
        assert await journal.read(SCOPE) == before
        assert len(await journal.outbox.pending("pyfly.audit.export")) == 1
