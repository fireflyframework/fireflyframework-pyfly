# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Same datasource names never authorize one application's units in another."""

import traceback
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy import Repository, SessionProvider
from pyfly.data.relational.sqlalchemy.session import ScopedAsyncSession
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import (
    IllegalTransactionStateError,
    Propagation,
    TransactionManagerRegistry,
    TransactionTemplate,
    UnexpectedRollbackError,
    infrastructure_unit,
    outside_transaction,
)
from pyfly.data.transaction.template import AutoUnit


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "manager_identity_items"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)


class Items(Repository[Item, str]):
    pass


@pytest.fixture
async def apps(tmp_path):
    contexts = []
    for name in ("a", "b"):
        ctx = ApplicationContext(
            Config(
                {
                    "pyfly": {
                        "data": {
                            "relational": {
                                "enabled": True,
                                "url": f"sqlite+aiosqlite:///{tmp_path / (name + '.db')}",
                                "ddl-auto": "none",
                            }
                        }
                    }
                }
            ),
            auto_configurations=[DataSourceAutoConfiguration, RelationalAutoConfiguration],
        )
        await ctx.start()
        contexts.append(ctx)
        async with ctx.get_bean(DataSourceRegistry).primary.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("INSERT INTO manager_identity_items VALUES (:id)"), {"id": name})
    try:
        yield contexts
    finally:
        for ctx in reversed(contexts):
            await ctx.stop()


def manager(ctx):
    return ctx.get_bean(TransactionManagerRegistry).get("primary")


async def names(ctx):
    async with ctx.get_bean(DataSourceRegistry).primary.engine.connect() as conn:
        return list(await conn.scalars(text("SELECT id FROM manager_identity_items ORDER BY id")))


async def wrong_call(ctx, action):
    target = manager(ctx)
    if action.startswith("infrastructure"):
        async with infrastructure_unit(target, read_only=action.endswith("read")) as session:
            await session.execute(text("SELECT id FROM manager_identity_items"))
    elif action.startswith("auto"):
        async with AutoUnit(target, read_only=action.endswith("read")) as unit:
            await unit.resource.execute(text("SELECT id FROM manager_identity_items"))
    elif action.startswith("boundary"):
        async with TransactionTemplate(target, propagation=Propagation(action.split(":")[1])).transaction():
            pass
    elif action == "provider.current":
        ctx.get_bean(SessionProvider).current()
    elif action == "provider.unit":
        async with ctx.get_bean(SessionProvider).unit(read_only=True):
            pass
    elif action == "scoped.session":
        session = ctx.get_bean(AsyncSession)
        await session.scalar(text("SELECT id FROM manager_identity_items"))
    else:
        repository = Items()
        repository._bind_transaction_managers(ctx.get_bean(TransactionManagerRegistry))
        if action == "repository.read":
            await repository.find_all()
        elif action == "repository.write":
            await repository.save(Item(id="foreign"))
        elif action == "repository.session":
            repository._require_session()
        elif action == "repository.stream":
            async for _ in repository.stream_all():
                pass


ACTIONS = [
    "infrastructure.read",
    "infrastructure.write",
    "auto.read",
    "auto.write",
    *("boundary:" + propagation.value for propagation in Propagation),
    "provider.current",
    "provider.unit",
    "scoped.session",
    "repository.read",
    "repository.write",
    "repository.session",
    "repository.stream",
]


@pytest.mark.parametrize("action", ACTIONS)
async def test_foreign_same_named_manager_is_refused_before_work_and_caught_refusal_rolls_back(apps, action):
    a, b = apps
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(manager(a)).transaction() as unit:
            await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('must-rollback')"))
            with pytest.raises(IllegalTransactionStateError, match="different transaction manager") as error:
                await wrong_call(b, action)
            assert str(error.value) == "A native Data unit belongs to a different transaction manager"
            assert unit.rollback_only
    assert await names(a) == ["a"]
    assert await names(b) == ["b"]


@pytest.mark.parametrize("held", ["read-auto", "write-auto", "read-bound", "suspended"])
@pytest.mark.parametrize("action", ["infrastructure.read", "auto.read", "boundary:REQUIRED", "repository.stream"])
async def test_scope_and_suspended_units_cannot_be_shadowed_by_foreign_manager(apps, held, action):
    a, b = apps

    @asynccontextmanager
    async def scope():
        if held == "suspended":
            async with TransactionTemplate(manager(a)).transaction() as unit:
                with outside_transaction():
                    yield unit
        elif held == "read-bound":
            async with TransactionTemplate(manager(a), read_only=True).transaction() as unit:
                yield unit
        else:
            async with AutoUnit(manager(a), read_only=held == "read-auto") as unit:
                yield unit

    async def attempt():
        async with scope() as unit:
            with pytest.raises(IllegalTransactionStateError, match="different transaction manager"):
                await wrong_call(b, action)
            assert unit.rollback_only

    if held == "read-auto":
        await attempt()
    else:
        with pytest.raises(UnexpectedRollbackError):
            await attempt()
    assert await names(a) == ["a"]
    assert await names(b) == ["b"]


async def test_same_manager_nested_units_and_distinct_names_remain_valid(apps):
    a, b = apps
    other = SqlAlchemyTransactionManager(
        sessionmaker=async_sessionmaker(b.get_bean(DataSourceRegistry).primary.engine), name="reporting"
    )
    async with TransactionTemplate(manager(a)).transaction() as outer:
        async with TransactionTemplate(manager(a), propagation=Propagation.NESTED).transaction() as nested:
            assert nested is outer
            await nested.resource.execute(text("INSERT INTO manager_identity_items VALUES ('nested')"))
        async with infrastructure_unit(other) as session:
            await session.execute(text("INSERT INTO manager_identity_items VALUES ('independent')"))
    assert await names(a) == ["a", "nested"]
    assert await names(b) == ["b", "independent"]


async def test_caught_foreign_refusal_uses_existing_native_savepoint_rollback(apps):
    a, b = apps
    async with TransactionTemplate(manager(a)).transaction() as outer:
        await outer.resource.execute(text("INSERT INTO manager_identity_items VALUES ('outer')"))
        async with TransactionTemplate(manager(a), propagation=Propagation.NESTED).transaction() as nested:
            await nested.resource.execute(text("INSERT INTO manager_identity_items VALUES ('nested-must-rollback')"))
            with pytest.raises(IllegalTransactionStateError, match="different transaction manager"):
                await wrong_call(b, "infrastructure.read")
        assert not outer.rollback_only
    assert await names(a) == ["a", "outer"]
    assert await names(b) == ["b"]


async def test_foreign_manager_refusal_does_not_chain_private_caught_errors(apps):
    a, b = apps
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(manager(a)).transaction():
            try:
                raise RuntimeError("PRIVATE_MANAGER_ERROR_CANARY")
            except RuntimeError:
                with pytest.raises(IllegalTransactionStateError) as captured:
                    await wrong_call(b, "infrastructure.read")
                assert "PRIVATE_MANAGER_ERROR_CANARY" not in "".join(traceback.format_exception(captured.value))


async def test_scoped_session_override_cannot_select_a_differently_named_foreign_unit(apps):
    a, b = apps
    factory = a.get_bean(DataSourceRegistry).primary.sessionmaker
    selected = ScopedAsyncSession.of(factory, datasource="secondary")
    foreign = SqlAlchemyTransactionManager(
        sessionmaker=async_sessionmaker(b.get_bean(DataSourceRegistry).primary.engine), name="secondary"
    )
    try:
        with pytest.raises(UnexpectedRollbackError):
            async with TransactionTemplate(foreign).transaction() as unit:
                await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('must-rollback')"))
                with pytest.raises(IllegalTransactionStateError, match="different transaction manager"):
                    await selected.scalar(text("SELECT id FROM manager_identity_items"))
        assert await names(b) == ["b"]
    finally:
        await selected.close()
    matching = ScopedAsyncSession.of(factory, datasource=manager(a).datasource)
    try:
        async with TransactionTemplate(manager(a)).transaction():
            assert await matching.scalar(text("SELECT id FROM manager_identity_items")) == "a"
    finally:
        await matching.close()


async def test_captured_repository_stream_refuses_resumption_inside_foreign_context(apps):
    a, b = apps
    async with a.get_bean(DataSourceRegistry).primary.engine.begin() as conn:
        await conn.execute(text("INSERT INTO manager_identity_items VALUES ('a-two')"))
    repository = Items()
    repository._bind_transaction_managers(a.get_bean(TransactionManagerRegistry))
    stream = repository.stream_all()
    try:
        assert (await anext(stream)).id in {"a", "a-two"}
        with pytest.raises(UnexpectedRollbackError):
            async with TransactionTemplate(manager(b)).transaction() as unit:
                await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('must-rollback')"))
                with pytest.raises(IllegalTransactionStateError, match="different transaction manager"):
                    await anext(stream)
        assert await names(a) == ["a", "a-two"]
        assert await names(b) == ["b"]
    finally:
        await stream.aclose()


async def test_session_provider_current_preserves_none_outside_a_unit(apps):
    _, b = apps
    assert b.get_bean(SessionProvider).current("unconfigured-name") is None


@pytest.mark.parametrize("participant", ["a", "b"])
async def test_other_application_rollback_wrapper_never_authorizes_foreign_unit(apps, participant):
    from pyfly.testing import RollbackTransaction

    a, b = apps
    async with RollbackTransaction(a) as rollback_a, RollbackTransaction(b) as rollback_b:
        taking_part = rollback_a if participant == "a" else rollback_b
        with taking_part.taking_part(), pytest.raises(UnexpectedRollbackError):
            async with TransactionTemplate(manager(a)).transaction() as unit:
                await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('must-rollback')"))
                with pytest.raises(IllegalTransactionStateError, match="different transaction manager"):
                    await wrong_call(b, "repository.read")
    assert await names(a) == ["a"]
    assert await names(b) == ["b"]


async def test_audit_with_frozen_original_manager_refuses_rollback_slice_participation(apps):
    from pyfly.audit import (
        AuditActor,
        AuditContext,
        AuditError,
        AuditOperation,
        AuditRecorder,
        AuditResource,
        AuditScope,
    )
    from pyfly.audit.sql import SqlAuditJournal
    from pyfly.testing import RollbackTransaction

    a, _ = apps
    original = manager(a)
    journal = SqlAuditJournal(original)
    await journal.start()
    scope = AuditScope(tenant_id="fixture", workspace_id="workspace", project_id="project")

    class Resolver:
        async def resolve(self, _):
            return AuditContext(
                scope=scope,
                actor=AuditActor(kind="service", subject_id="verified"),
                resource=AuditResource(kind="item", resource_id="fixture"),
            )

    recorder = AuditRecorder(journal, Resolver(), transaction_manager=original, modules=frozenset({"data"}))
    operation = AuditOperation(module="data", action="fixture.changed")
    async with RollbackTransaction(a):
        with pytest.raises(UnexpectedRollbackError):
            async with TransactionTemplate(manager(a)).transaction() as unit:
                await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('must-rollback')"))
                with pytest.raises(AuditError, match="same active native SQL write unit"):
                    await recorder.state(operation)
    assert await names(a) == ["a"]
    assert not await journal.read(scope)
    async with TransactionTemplate(original).transaction() as unit:
        await unit.resource.execute(text("INSERT INTO manager_identity_items VALUES ('manual-native')"))
        await recorder.state(operation)
    assert len(await journal.read(scope)) == 1
    await journal.stop()
