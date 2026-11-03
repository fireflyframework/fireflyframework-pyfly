# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native Data + Audit transactions on independent temporary SQLite contexts."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from pyfly.agentic.pipeline import CheckpointRecord
from pyfly.agentic.pipeline.durable import DurableBinding, DurableConflict, DurableRunKey, DurableStateError
from pyfly.agentic.pipeline.durable.sql import SqlDurableRunStore
from pyfly.audit import AuditJournal
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.models import AuditActor, AuditScope
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import SessionProvider, SqlAlchemyTransactionManager

AUTHOR = AuditActor(kind="human", subject_id="author")
REVIEWER = AuditActor(kind="human", subject_id="reviewer")
SCOPE = AuditScope(tenant_id="tenant", workspace_id="workspace", project_id="project")
KEY = DurableRunKey(scope=SCOPE, run_id="run")
BINDING = DurableBinding(
    pipeline_name="flow",
    definition_id="definition",
    definition_revision=1,
    graph_version="v1",
    graph_digest="a" * 64,
    plan_digest="b" * 64,
    node_ids=("a", "wait", "b"),
    pause_node_id="wait",
)
INPUT = {"original": {"question": "fixture"}}


@asynccontextmanager
async def opened(path, *, clock=None, create=True):
    config = Config(
        {
            "pyfly": {
                "audit": {"enabled": True},
                "data": {
                    "relational": {
                        "enabled": True,
                        "url": "sqlite+aiosqlite:///" + str(path),
                        "ddl-auto": "create" if create else "none",
                        "sqlite": {"journal-mode": "WAL", "synchronous": "FULL", "busy-timeout": 5000},
                    }
                },
            }
        }
    )
    context = ApplicationContext(
        config, auto_configurations=[DataSourceAutoConfiguration, RelationalAutoConfiguration, AuditAutoConfiguration]
    )
    await context.start()
    try:
        manager = context.get_bean(SqlAlchemyTransactionManager)
        sessions = context.get_bean(SessionProvider)
        journal = context.get_bean(AuditJournal)
        store = SqlDurableRunStore(
            manager, sessions, journal, create_tables=create, **({"clock": clock} if clock else {})
        )
        await store.start()
        try:
            yield store, sessions, journal
        finally:
            await store.stop()
    finally:
        await context.stop()


def record(count, *, paused=False):
    names = BINDING.node_ids[:count]
    values = {
        name: {
            "node_id": name,
            "output": {"reason": "approve"} if name == "wait" else {"answer": 7},
            "success": True,
            "skipped": False,
        }
        for name in names
    }
    return CheckpointRecord(
        pipeline_name="flow",
        run_id="run",
        node_id=names[-1],
        sequence=count,
        completed_nodes=list(names),
        state={"inputs": INPUT, "results": values, "shared_state": None},
        paused=paused,
        pause_reason="approve" if paused else None,
    )


async def waiting(store):
    await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
    lease = await store.claim(KEY, BINDING, owner="worker-one", expected_generation=1)
    await store.checkpoint(lease, record(1))
    envelope = await store.checkpoint(lease, record(2, paused=True))
    return envelope, lease


async def decide(store, envelope, **kwargs):
    return await store.decide(
        KEY,
        BINDING,
        task_id=envelope.task.task_id,
        expected_generation=1,
        actor=REVIEWER,
        decision="approve",
        idempotency_key="request-one",
        policy_digest="c" * 64,
        **kwargs,
    )


async def test_pause_decision_and_new_claim_survive_native_context_restart(tmp_path):
    path = tmp_path / "restart.db"
    async with opened(path) as (store, _, journal):
        envelope, old = await waiting(store)
        assert envelope.status == "waiting_approval" and envelope.lease is None
        assert (await journal.verify(SCOPE)).head.sequence == envelope.version
    async with opened(path, create=False) as (store, _, journal):
        envelope = await store.load(KEY, BINDING)
        assert envelope.original_input == INPUT and envelope.checkpoint.state["results"]["a"]["output"] == {"answer": 7}
        approved = await decide(store, envelope)
        assert approved.status == "queued" and approved.generation == 2 and approved.version > envelope.version
        lease = await store.claim(KEY, BINDING, owner="worker-two", expected_generation=2)
        restored = await store.checkpoint_for(lease)
        assert restored.paused is False and restored.completed_nodes == ["a", "wait"]
        with pytest.raises(DurableConflict):
            await store.checkpoint(old, record(3))
        done = await store.checkpoint(lease, record(3))
        finished = await store.finish(lease, outcome="succeeded")
        assert done.checkpoint.node_id == "b" and finished.status == "succeeded"
        events = await store.events(KEY, BINDING)
        assert [e.sequence for e in events] == list(range(1, finished.version + 1))
        assert (await journal.verify(SCOPE)).head.sequence == finished.version


async def test_one_decision_wins_two_contexts_and_identical_retry_is_stable(tmp_path):
    path = tmp_path / "race.db"
    async with opened(path) as (first, _, _):
        envelope, _ = await waiting(first)
        async with opened(path, create=False) as (second, _, _):
            results = await asyncio.gather(decide(first, envelope), decide(second, envelope), return_exceptions=True)
            assert all(not isinstance(x, BaseException) for x in results)
            assert results[0].version == results[1].version
            before = (await first.load(KEY, BINDING)).version
            with pytest.raises(DurableConflict):
                await first.decide(
                    KEY,
                    BINDING,
                    task_id=envelope.task.task_id,
                    expected_generation=1,
                    actor=REVIEWER,
                    decision="reject",
                    idempotency_key="request-one",
                    policy_digest="c" * 64,
                )
            assert (await first.load(KEY, BINDING)).version == before


async def test_audit_or_later_outer_failure_rolls_back_decision_and_event(tmp_path):
    async with opened(tmp_path / "rollback.db") as (store, sessions, journal):
        envelope, _ = await waiting(store)
        before = await journal.head(SCOPE)
        with pytest.raises(RuntimeError, match="outer failure"):
            async with sessions.unit() as session:
                await decide(store, envelope)
                await session.execute(text("SELECT 1"))
                raise RuntimeError("outer failure")
        current = await store.load(KEY, BINDING)
        assert current == envelope and await journal.head(SCOPE) == before
        assert len(await store.events(KEY, BINDING)) == envelope.version


async def test_bad_scope_plan_self_review_and_forged_pause_cannot_mutate(tmp_path):
    async with opened(tmp_path / "boundary.db") as (store, _, journal):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, BINDING, owner="worker", expected_generation=1)
        for bad in (record(2, paused=True), record(3), record(1).model_copy(update={"state": {"inputs": "other"}})):
            with pytest.raises(DurableStateError):
                await store.checkpoint(lease, bad)
        with pytest.raises(DurableStateError):
            await store.load(KEY.model_copy(update={"scope": SCOPE.model_copy(update={"tenant_id": "other"})}), BINDING)
        with pytest.raises(DurableStateError):
            await store.load(KEY, BINDING.model_copy(update={"plan_digest": "f" * 64}))
        await store.checkpoint(lease, record(1))
        envelope = await store.checkpoint(lease, record(2, paused=True))
        with pytest.raises(DurableConflict):
            await store.decide(
                KEY,
                BINDING,
                task_id=envelope.task.task_id,
                expected_generation=1,
                actor=AUTHOR,
                decision="approve",
                idempotency_key="self",
                policy_digest="c" * 64,
            )
        assert (await store.load(KEY, BINDING)) == envelope
        assert (await journal.verify(SCOPE)).head.sequence == envelope.version


async def test_renewal_and_expired_claim_never_replay_unknown_effect(tmp_path):
    now = [datetime(2026, 11, 2, tzinfo=UTC)]
    async with opened(tmp_path / "expiry.db", clock=lambda: now[0]) as (store, _, _):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, BINDING, owner="worker", expected_generation=1, ttl_seconds=10)
        now[0] += timedelta(seconds=5)
        renewed = await store.renew(lease, ttl_seconds=10)
        assert renewed.epoch == lease.epoch and renewed.expires_at > lease.expires_at
        now[0] += timedelta(seconds=11)
        with pytest.raises(DurableConflict):
            await store.checkpoint(renewed, record(1))
        with pytest.raises(DurableConflict):
            await store.renew(renewed)
        with pytest.raises(DurableConflict):
            await store.claim(KEY, BINDING, owner="replacement", expected_generation=1)
        assert (await store.load(KEY, BINDING)).status == "running"
        assert (await store.finish(renewed, outcome="unknown")).status == "unknown"


async def test_decision_actor_policy_and_generation_are_part_of_idempotency(tmp_path):
    async with opened(tmp_path / "identity.db") as (store, _, journal):
        envelope, _ = await waiting(store)
        approved = await decide(store, envelope)
        head = await journal.head(SCOPE)
        for change in (
            {"actor": AuditActor(kind="human", subject_id="other-reviewer")},
            {"policy_digest": "d" * 64},
            {"expected_generation": 2},
            {"idempotency_key": "other-request"},
        ):
            params = {
                "task_id": envelope.task.task_id,
                "expected_generation": 1,
                "actor": REVIEWER,
                "decision": "approve",
                "idempotency_key": "request-one",
                "policy_digest": "c" * 64,
            } | change
            with pytest.raises(DurableConflict):
                await store.decide(KEY, BINDING, **params)
        assert await store.load(KEY, BINDING) == approved and await journal.head(SCOPE) == head


async def test_events_detect_valid_json_tampering_and_claim_state_mutation(tmp_path):
    import json

    from sqlalchemy import select, update

    from pyfly.agentic.pipeline.durable.sql import digest
    from pyfly.data.relational.framework_schema import agentic_durable_events, agentic_durable_runs

    async with opened(tmp_path / "corrupt.db") as (store, sessions, _):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, BINDING, owner="worker", expected_generation=1)
        with pytest.raises(DurableConflict):
            await store.validate_claim(lease.model_copy(update={"owner": "forged"}))
        async with sessions.unit() as session:
            payload = json.loads(
                (
                    await session.execute(
                        select(agentic_durable_events.c.payload).where(agentic_durable_events.c.sequence == 1)
                    )
                ).scalar_one()
            )
            payload["kind"] = "approved"
            await session.execute(
                update(agentic_durable_events)
                .where(agentic_durable_events.c.sequence == 1)
                .values(payload=json.dumps(payload))
            )
        with pytest.raises(DurableStateError) as error:
            await store.events(KEY, BINDING)
        assert "fixture-canary" not in str(error.value)
        value = (await store.load(KEY, BINDING)).model_dump(mode="json")
        value["lease"]["epoch"] = 999
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
        async with sessions.unit() as session:
            await session.execute(update(agentic_durable_runs).values(payload=payload, payload_digest=digest(value)))
        with pytest.raises(DurableStateError):
            await store.load(KEY, BINDING)


async def test_boolean_output_substitution_and_reopening_approved_pause_are_denied(tmp_path):
    async with opened(tmp_path / "closed-cursor.db") as (store, _, _):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, BINDING, owner="first", expected_generation=1)
        first = record(1)
        first.state["results"]["a"]["output"] = {"answer": 1}
        await store.checkpoint(lease, first)
        forged = record(2, paused=True)
        forged.state["results"]["a"]["output"] = {"answer": True}
        with pytest.raises(DurableStateError):
            await store.checkpoint(lease, forged)
        paused = record(2, paused=True)
        paused.state["results"]["a"]["output"] = {"answer": 1}
        value = await store.checkpoint(lease, paused)
        await decide(store, value)
        next_claim = await store.claim(KEY, BINDING, owner="second", expected_generation=2)
        with pytest.raises(DurableStateError):
            await store.checkpoint(next_claim, paused)


async def test_caught_joined_audit_failure_still_marks_outer_transaction_rollback_only(tmp_path, monkeypatch):
    from pyfly.data.transaction import UnexpectedRollbackError

    async with opened(tmp_path / "caught.db") as (store, sessions, journal):
        value, _ = await waiting(store)
        head = await journal.head(SCOPE)
        append = journal.append

        async def append_then_fail(event):
            await append(event)
            raise RuntimeError("private-fault-canary")

        monkeypatch.setattr(journal, "append", append_then_fail)
        with pytest.raises(UnexpectedRollbackError) as error:
            async with sessions.unit():
                with pytest.raises(RuntimeError):
                    await decide(store, value)
        assert "private-fault-canary" not in str(error.value)
        assert await store.load(KEY, BINDING) == value and await journal.head(SCOPE) == head


async def test_waiting_cancellation_fences_approval_and_is_exactly_idempotent(tmp_path):
    async with opened(tmp_path / "cancelled.db") as (store, _, journal):
        waiting_value, old_claim = await waiting(store)
        cancelled = await store.cancel(KEY, BINDING, actor=AUTHOR, expected_version=waiting_value.version)
        assert cancelled.status == "cancelled" and cancelled.generation == waiting_value.generation + 1
        assert cancelled.epoch > old_claim.epoch
        assert await store.cancel(KEY, BINDING, actor=AUTHOR, expected_version=waiting_value.version) == cancelled
        with pytest.raises(DurableConflict):
            await decide(store, waiting_value)
        with pytest.raises(DurableConflict):
            await store.claim(KEY, BINDING, owner="new", expected_generation=cancelled.generation)
        with pytest.raises(DurableConflict):
            await store.checkpoint(old_claim, record(3))
        with pytest.raises(DurableConflict):
            await store.cancel(KEY, BINDING, actor=REVIEWER, expected_version=waiting_value.version)
        assert (await journal.verify(SCOPE)).head.sequence == cancelled.version


async def test_running_effect_cannot_be_reported_cancelled(tmp_path):
    async with opened(tmp_path / "active.db") as (store, _, _):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        await store.claim(KEY, BINDING, owner="active", expected_generation=1)
        value = await store.load(KEY, BINDING)
        with pytest.raises(DurableConflict):
            await store.cancel(KEY, BINDING, actor=AUTHOR, expected_version=value.version)
        assert await store.load(KEY, BINDING) == value


def test_malformed_claim_and_nested_binding_emit_no_serialization_warning():
    import warnings

    from pyfly.agentic.pipeline.durable import BoundDurableCheckpointer, DurableClaim
    from pyfly.agentic.pipeline.durable.sql import checked

    canary = "private-claim-warning-canary"
    valid = DurableClaim(
        key=KEY,
        binding=BINDING,
        owner="worker",
        generation=1,
        epoch=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    revalidated = checked(DurableClaim, valid)
    assert revalidated is not valid and revalidated.binding is not valid.binding
    assert revalidated.key.scope is not valid.key.scope
    for forged in (
        valid.model_copy(update={"owner": {"token": canary}}),
        valid.model_copy(update={"binding": BINDING.model_copy(update={"graph_digest": {"token": canary}})}),
    ):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with pytest.raises(DurableStateError) as error:
                checked(DurableClaim, forged)
            assert canary not in str(error.value)
            assert captured == []
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with pytest.raises(DurableStateError):
                BoundDurableCheckpointer(None, forged)
            assert captured == []


async def test_malformed_checkpoint_emits_no_warning_before_atomic_validation(tmp_path):
    import warnings

    async with opened(tmp_path / "checkpoint-warning.db") as (store, _, journal):
        await store.create(KEY, BINDING, INPUT, actor=AUTHOR)
        lease = await store.claim(KEY, BINDING, owner="worker", expected_generation=1)
        head = await journal.head(SCOPE)
        malformed = record(1).model_copy(update={"sequence": {"token": "private-checkpoint-warning-canary"}})
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with pytest.raises(DurableStateError) as error:
                await store.checkpoint(lease, malformed)
            assert "private-checkpoint-warning-canary" not in str(error.value)
            assert captured == []
        assert await journal.head(SCOPE) == head


async def test_malformed_paused_retry_is_rejected_before_any_serializer_warning(tmp_path):
    import warnings

    async with opened(tmp_path / "paused-warning.db") as (store, _, journal):
        value, lease = await waiting(store)
        malformed = record(2, paused=True).model_copy(
            update={"pause_reason": {"token": "private-paused-warning-canary"}}
        )
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with pytest.raises(DurableStateError):
                await store.checkpoint(lease, malformed)
            assert captured == []
        assert await store.load(KEY, BINDING) == value
