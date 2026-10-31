# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Semantic recorder boundaries on real native SQLite and PostgreSQL transactions."""

import asyncio
import importlib

import pytest
from sqlalchemy import Column, MetaData, String, Table, func, insert, select

from pyfly.audit import AuditActor, AuditError, AuditResource, AuditScope
from pyfly.audit.sql import SqlAuditJournal
from pyfly.data.transaction import (
    Propagation,
    TransactionTemplate,
    UnexpectedRollbackError,
    current_unit_of_work,
    resolve_manager,
)
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")
SCOPE = AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project")
orders = Table("audit_recording_orders", MetaData(), Column("id", String(64), primary_key=True))


def api():
    module = importlib.import_module("pyfly.audit")
    assert hasattr(module, "AuditRecorder")
    return module


async def setup(backend: RelationalBackend, *, broken=False):
    contract = api()
    engine = backend.create_engine()
    journal = SqlAuditJournal(engine)
    await journal.start()
    async with engine.begin() as connection:
        await connection.run_sync(orders.create)
    manager = resolve_manager(engine)

    class Resolver:
        async def resolve(self, operation):
            if broken:
                raise ValueError("fixture private resolver error")
            return contract.AuditContext(
                scope=SCOPE,
                actor=AuditActor(kind="service", subject_id="verified"),
                resource=AuditResource(kind="order", resource_id="authoritative-order", revision=1),
            )

    return (
        manager,
        journal,
        contract.AuditRecorder(
            journal,
            Resolver(),
            transaction_manager=manager,
            modules=frozenset({"data", "web", "agentic", "eda", "security"}),
        ),
    )


async def test_business_state_and_fact_share_rollback_and_native_outbox(relational_backend: RelationalBackend):
    manager, journal, recorder = await setup(relational_backend)
    operation = api().AuditOperation(module="data", action="order.saved", resource_kind="order")
    with pytest.raises(RuntimeError, match="abort"):
        async with TransactionTemplate(manager).transaction() as unit:
            await unit.resource.execute(insert(orders).values(id="order"))
            await recorder.state(operation)
            raise RuntimeError("abort")
    assert not await journal.read(SCOPE)
    assert not await journal.outbox.pending("pyfly.audit.export")
    async with TransactionTemplate(manager).transaction() as unit:
        assert await unit.resource.scalar(select(func.count()).select_from(orders)) == 0
        await unit.resource.execute(insert(orders).values(id="order"))
        await recorder.state(operation)
    entries = await journal.read(SCOPE)
    assert len(entries) == len(await journal.outbox.pending("pyfly.audit.export")) == 1
    assert entries[0].event.resource.resource_id == "authoritative-order"
    assert entries[0].event.event_type == "data.order.saved"


async def test_caught_resolver_failure_marks_actual_business_unit_rollback_only(relational_backend: RelationalBackend):
    manager, journal, recorder = await setup(relational_backend, broken=True)
    with pytest.raises(UnexpectedRollbackError) as rolled_back:
        async with TransactionTemplate(manager).transaction() as unit:
            await unit.resource.execute(insert(orders).values(id="order"))
            with pytest.raises(AuditError) as failed:
                await recorder.state(api().AuditOperation(module="data", action="order.saved"))
            assert "fixture private resolver error" not in str(failed.value)
    assert "fixture private resolver error" not in str(rolled_back.value)
    assert not await journal.read(SCOPE)
    async with TransactionTemplate(manager).transaction() as unit:
        assert await unit.resource.scalar(select(func.count()).select_from(orders)) == 0


async def test_context_resolver_cannot_leak_a_raw_audit_error(relational_backend: RelationalBackend):
    manager, journal, recorder = await setup(relational_backend)

    class BrokenResolver:
        async def resolve(self, operation):
            raise AuditError("fixture private resolver value")

    recorder.resolver = BrokenResolver()
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(manager).transaction():
            with pytest.raises(AuditError) as error:
                await recorder.state(api().AuditOperation(module="data", action="order.saved"))
            assert "fixture private" not in str(error.value)
    assert not await journal.read(SCOPE)


async def test_forged_operation_cannot_disable_capture_and_commit_unaudited_state(
    relational_backend: RelationalBackend,
):
    manager, journal, recorder = await setup(relational_backend)
    forged = api().AuditOperation.model_construct(module=None, action="write")
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(manager).transaction() as unit:
            await unit.resource.execute(insert(orders).values(id="forged"))
            with pytest.raises(AuditError):
                await recorder.state(forged)
    assert not await journal.read(SCOPE)


async def test_state_refuses_read_only_and_missing_units(relational_backend: RelationalBackend):
    manager, journal, recorder = await setup(relational_backend)
    operation = api().AuditOperation(module="data", action="order.saved")
    with pytest.raises(AuditError):
        await recorder.state(operation)
    with pytest.raises(AuditError):
        async with TransactionTemplate(manager).transaction(read_only=True):
            await recorder.state(operation)
    assert not await journal.read(SCOPE)


async def test_effect_intent_commits_before_io_and_terminal_context_is_frozen(relational_backend: RelationalBackend):
    from pyfly.audit import AuditUsage

    manager, journal, recorder = await setup(relational_backend)
    contract = api()
    operation = contract.AuditOperation(module="agentic", action="model.request", resource_key="approved-model")
    async with recorder.effect(operation) as effect:
        assert current_unit_of_work() is None
        entries = await journal.read(SCOPE)
        assert len(entries) == 1 and entries[0].event.outcome == "intent"
        assert effect.intent == entries[0]
        effect.enrich(
            usage=AuditUsage(status="measured", input_tokens=3, output_tokens=2),
            references=(AuditResource(kind="rate", resource_id="approved-rate", revision=2),),
        )
        with pytest.raises(TypeError):
            effect.enrich(actor=AuditActor(kind="service", subject_id="forged"))
    entries = await journal.read(SCOPE)
    assert [entry.event.outcome for entry in entries] == ["intent", "observed"]
    assert entries[0].event.actor == entries[1].event.actor
    assert entries[0].event.scope == entries[1].event.scope
    assert entries[0].event.resource == entries[1].event.resource
    assert entries[1].event.correlation.causation_event_id == entries[0].event.event_id
    assert entries[1].event.usage.input_tokens == 3
    assert entries[1].event.usage.spent_microusd is None


@pytest.mark.parametrize("suspended", [False, True])
async def test_external_effect_refuses_active_or_suspended_business_writer(
    relational_backend: RelationalBackend, suspended
):
    manager, journal, recorder = await setup(relational_backend)
    operation = api().AuditOperation(module="agentic", action="tool.call")
    async with TransactionTemplate(manager).transaction() as unit:
        await unit.resource.execute(insert(orders).values(id="order"))
        if suspended:
            async with TransactionTemplate(manager).transaction(propagation=Propagation.NOT_SUPPORTED):
                with pytest.raises(AuditError):
                    async with recorder.effect(operation):
                        pytest.fail("External effect must not be admitted")
        else:
            with pytest.raises(AuditError):
                async with recorder.effect(operation):
                    pytest.fail("External effect must not be admitted")
    assert not await journal.read(SCOPE)


async def test_post_yield_failure_is_unknown_and_nonretryable_without_raw_error(relational_backend: RelationalBackend):
    _, journal, recorder = await setup(relational_backend)
    with pytest.raises(api().AuditEffectUncertain) as uncertain:
        async with recorder.effect(api().AuditOperation(module="agentic", action="tool.call")):
            raise ValueError("fixture private provider response")
    assert uncertain.value.retryable is False
    assert "fixture private provider response" not in str(uncertain.value)
    entries = await journal.read(SCOPE)
    assert [entry.event.outcome for entry in entries] == ["intent", "unknown"]
    assert "fixture private provider response" not in str(entries)


async def test_independent_observation_rejects_active_writer_and_has_no_business_atomicity_claim(
    relational_backend: RelationalBackend,
):
    manager, journal, recorder = await setup(relational_backend)
    operation = api().AuditOperation(module="web", action="request.denied")
    async with TransactionTemplate(manager).transaction():
        with pytest.raises(AuditError):
            await recorder.observation(operation, outcome="denied")
    await recorder.observation(operation, outcome="denied")
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["denied"]


async def test_wrong_manager_state_failure_poisoning_cannot_commit_caught_unaudited_write(
    relational_backend: RelationalBackend,
):
    _, journal, recorder = await setup(relational_backend)
    other = resolve_manager(relational_backend.create_engine())
    with pytest.raises(UnexpectedRollbackError):
        async with TransactionTemplate(other).transaction() as unit:
            await unit.resource.execute(insert(orders).values(id="wrong-manager"))
            with pytest.raises(AuditError):
                await recorder.state(api().AuditOperation(module="data", action="order.saved"))
    assert not await journal.read(SCOPE)


async def test_cancelled_effect_records_unknown_and_preserves_cancellation(relational_backend: RelationalBackend):
    _, journal, recorder = await setup(relational_backend)
    with pytest.raises(asyncio.CancelledError):
        async with recorder.effect(api().AuditOperation(module="agentic", action="model.request")):
            raise asyncio.CancelledError()
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["intent", "unknown"]


async def test_same_server_command_cannot_readmit_even_after_observed_or_uncertain_effect(
    relational_backend: RelationalBackend,
):
    from pyfly.audit import AuditAdmissionRefused

    _, journal, recorder = await setup(relational_backend)
    operation = api().AuditOperation(module="agentic", action="tool.call")
    calls = []
    async with recorder.effect(operation, admission_key="server-command"):
        calls.append(True)
    with pytest.raises(AuditAdmissionRefused) as refused:
        async with recorder.effect(operation, admission_key="server-command"):
            calls.append(True)
    assert refused.value.retryable is False
    assert calls == [True]
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["intent", "observed"]


async def test_cancellation_while_retaining_observation_finishes_unknown_without_replay(
    relational_backend: RelationalBackend, monkeypatch
):
    _, journal, recorder = await setup(relational_backend)
    entered, release = asyncio.Event(), asyncio.Event()
    append = journal.append

    async def wait_for_outcome(event):
        if event.outcome == "observed":
            entered.set()
            await release.wait()
        return await append(event)

    monkeypatch.setattr(journal, "append", wait_for_outcome)
    effects = []

    async def run():
        async with recorder.effect(api().AuditOperation(module="agentic", action="tool.call")):
            effects.append(True)

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert effects == [True]
    assert (await journal.read(SCOPE))[-1].event.outcome == "unknown"


async def test_post_io_storage_failure_retains_only_committed_intent_and_is_nonretryable(
    relational_backend: RelationalBackend,
):
    manager, journal, recorder = await setup(relational_backend)
    with pytest.raises(api().AuditEffectUncertain) as uncertain:
        async with (
            recorder.effect(api().AuditOperation(module="agentic", action="tool.call")),
            TransactionTemplate(manager).transaction() as unit,
        ):
            from sqlalchemy import text

            if relational_backend.is_embedded:
                await unit.resource.execute(
                    text(
                        "CREATE TRIGGER fixture_reject BEFORE INSERT ON pyfly_audit_entries "
                        "BEGIN SELECT RAISE(ABORT, 'fixture private database error'); END"
                    )
                )
            else:
                await unit.resource.execute(
                    text(
                        "CREATE FUNCTION fixture_reject() RETURNS trigger AS $$ "
                        "BEGIN RAISE EXCEPTION 'fixture private database error'; END $$ LANGUAGE plpgsql"
                    )
                )
                await unit.resource.execute(
                    text(
                        "CREATE TRIGGER fixture_reject BEFORE INSERT ON pyfly_audit_entries "
                        "FOR EACH ROW EXECUTE FUNCTION fixture_reject()"
                    )
                )
    assert uncertain.value.retryable is False
    assert "fixture private database error" not in str(uncertain.value)
    entries = await journal.read(SCOPE)
    assert len(entries) == 1 and entries[0].event.outcome == "intent"
    assert len(await journal.outbox.pending("pyfly.audit.export")) == 1
