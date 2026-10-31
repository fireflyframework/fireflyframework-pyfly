# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Strict opt-in native outbox audit and durable effect admission on real SQL."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from pyfly.audit import AuditActor, AuditContext, AuditRecorder, AuditResource, AuditScope
from pyfly.audit._infrastructure import audit_infrastructure
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.ports import AuditError
from pyfly.audit.recording_auto_configuration import AuditRecordingAutoConfiguration
from pyfly.audit.recording_context import AuditContextResolver
from pyfly.audit.sql import SqlAuditJournal
from pyfly.container.bean import bean
from pyfly.container.stereotypes import configuration
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.transaction import UnexpectedRollbackError, infrastructure_unit, resolve_manager
from pyfly.eda.outbox import OutboxRelay, OutboxTables, SqlOutboxStore
from pyfly.eda.ports.outbound import EventPublisher
from pyfly.eda.types import ErrorStrategy, EventEnvelope
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")
SCOPE = AuditScope(tenant_id="fixture", workspace_id="workspace", project_id="project")
PRIVATE = "PRIVATE_EDA_CONTENT_CANARY"


def bind(target, factory):
    method = getattr(target, "_bind_audit_recorder", None)
    assert callable(method), "Native EDA components must support application-owned audit enrollment"
    method(factory)


async def setup(backend, *, broken=False):
    engine = backend.create_engine()
    journal = SqlAuditJournal(engine)
    await journal.start()

    class Resolver:
        async def resolve(self, operation):
            if broken:
                raise RuntimeError(PRIVATE)
            return AuditContext(
                scope=SCOPE,
                actor=AuditActor(kind="service", subject_id="verified-fixture"),
                resource=AuditResource(
                    kind=operation.resource_kind or "delivery", resource_id=operation.resource_key or "fixture"
                ),
            )

    recorder = AuditRecorder(
        journal, Resolver(), transaction_manager=resolve_manager(engine), modules=frozenset({"eda"})
    )
    store = SqlOutboxStore(engine)

    def factory():
        return recorder

    bind(store, factory)
    await store.start()
    return engine, journal, store, recorder, factory


def envelope():
    return EventEnvelope(
        event_type="fixture.changed", destination="fixture", payload={"secret": PRIVATE}, headers={"private": PRIVATE}
    )


async def relay_for(store, factory, handler, *, strategy=ErrorStrategy.DEAD_LETTER, **options):
    relay = OutboxRelay(store, group="worker", register=False, transactional=False, error_strategy=strategy, **options)
    bind(relay, factory)
    relay.subscribe("fixture.changed", handler)
    return relay


async def test_sql_enqueue_and_audit_share_native_rollback_and_exclude_payload(relational_backend: RelationalBackend):
    engine, journal, store, _, _ = await setup(relational_backend)
    with pytest.raises(RuntimeError, match="abort"):
        async with infrastructure_unit(engine):
            await store.append(envelope(), groups=["worker"])
            raise RuntimeError("abort")
    assert not await journal.read(SCOPE)
    assert not await store.pending("worker")
    await store.append(envelope(), groups=["worker"])
    entries = await journal.read(SCOPE)
    assert [entry.event.event_type for entry in entries] == ["eda.outbox.enqueued"]
    assert PRIVATE not in (await journal.export(SCOPE, through_sequence=1)).decode()
    assert len(await store.pending("worker")) == 1


async def test_caught_enqueue_context_failure_cannot_commit_unaudited_event(relational_backend: RelationalBackend):
    engine, journal, store, _, _ = await setup(relational_backend, broken=True)
    with pytest.raises(UnexpectedRollbackError) as failed:
        async with infrastructure_unit(engine):
            with pytest.raises(AuditError):
                await store.append(envelope(), groups=["worker"])
    assert PRIVATE not in str(failed.value)
    assert not await journal.read(SCOPE)
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(store.tables.events)) == 0


async def test_delivery_commits_intent_before_handler_and_records_observed(relational_backend: RelationalBackend):
    _, journal, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])
    called = []

    async def handler(value):
        entries = await journal.read(SCOPE)
        assert entries[-1].event.outcome == "intent"
        called.append(value.payload["secret"])

    relay = await relay_for(store, factory, handler)
    assert await relay.run_once() == 1
    assert called == [PRIVATE]
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "observed"]
    assert PRIVATE not in (await journal.export(SCOPE, through_sequence=3)).decode()
    assert not await store.pending("worker")


@pytest.mark.parametrize("strategy", list(ErrorStrategy))
async def test_unknown_delivery_is_never_replayed_under_any_error_strategy(
    relational_backend: RelationalBackend, strategy, caplog
):
    _, journal, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])
    calls = []

    async def handler(_):
        calls.append("effect")
        raise RuntimeError(PRIVATE)

    relay = await relay_for(store, factory, handler, strategy=strategy)
    await relay.run_once()
    for _ in range(2):
        await relay.run_once()
    assert calls == ["effect"]
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "unknown"]
    assert not await store.pending("worker")
    assert relay.counters.delivered == 0
    assert relay.counters.dead_lettered == 1
    assert PRIVATE not in caplog.text


async def test_ack_failure_and_new_relay_cannot_repeat_effect(relational_backend: RelationalBackend, monkeypatch):
    _, journal, store, _, factory = await setup(relational_backend)
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    monkeypatch.setattr(store, "_clock", lambda: instant)
    await store.append(envelope(), groups=["worker"])
    calls = []

    async def handler(_):
        calls.append("effect")

    relay = await relay_for(store, factory, handler)
    original = store.complete

    async def fail_complete(_):
        raise RuntimeError("fixture settle unavailable")

    monkeypatch.setattr(store, "complete", fail_complete)
    with pytest.raises(RuntimeError):
        await relay.run_once()
    monkeypatch.setattr(store, "complete", original)
    instant += timedelta(hours=1)
    restarted = await relay_for(store, factory, handler)
    await restarted.run_once()
    assert calls == ["effect"]
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "observed"]
    assert not await store.pending("worker")


async def test_own_journal_infrastructure_skips_recording_without_global_suppression(
    relational_backend: RelationalBackend,
):
    _, journal, store, _, factory = await setup(relational_backend)
    calls = []

    async def handler(_):
        calls.append(True)

    relay = await relay_for(store, factory, handler)
    with audit_infrastructure(journal):
        await store.append(envelope(), groups=["worker"])
        await relay.run_once()
    assert calls == [True]
    assert not await journal.read(SCOPE)


async def test_external_dead_letter_store_is_refused_before_any_claim(relational_backend: RelationalBackend):
    _, journal, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])
    calls = []

    class External:
        async def add(self, _):
            calls.append("external-dlq")

    async def handler(_):
        calls.append("handler")

    relay = await relay_for(store, factory, handler, dead_letter_store=External())
    with pytest.raises(AuditError, match="external dead-letter"):
        await relay.run_once()
    assert calls == []
    assert (await store.pending("worker"))[0].attempts == 0
    assert len(await journal.read(SCOPE)) == 1


async def test_held_business_unit_is_refused_before_claim(relational_backend: RelationalBackend):
    engine, _, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])

    async def handler(_):
        pytest.fail("External handler admitted inside business unit")

    relay = await relay_for(store, factory, handler)
    async with infrastructure_unit(engine):
        with pytest.raises(AuditError, match="no held"):
            await relay.run_once()
    assert (await store.pending("worker"))[0].attempts == 0


async def test_lost_claim_before_intent_cannot_admit_handler(relational_backend: RelationalBackend, monkeypatch):
    _, journal, store, _, factory = await setup(relational_backend)
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    monkeypatch.setattr(store, "_clock", lambda: instant)
    await store.append(envelope(), groups=["worker"])
    stale = (await store.claim("worker", limit=1, lease=timedelta(seconds=1), owner="stale"))[0]
    instant += timedelta(seconds=2)
    fresh = (await store.claim("worker", limit=1, lease=timedelta(minutes=10), owner="fresh"))[0]
    calls = []

    async def handler(_):
        calls.append("effect")

    relay = await relay_for(store, factory, handler)
    await relay._deliver(stale)
    assert calls == []
    assert len(await journal.read(SCOPE)) == 1
    assert await relay._deliver(fresh)
    assert calls == ["effect"]


async def test_expired_claim_after_intent_stays_fenced_without_handler(
    relational_backend: RelationalBackend, monkeypatch
):
    _, journal, store, recorder, factory = await setup(relational_backend)
    instant = datetime(2026, 10, 31, tzinfo=UTC)
    monkeypatch.setattr(store, "_clock", lambda: instant)
    await store.append(envelope(), groups=["worker"])
    independent = recorder._independent

    async def advance_after_intent(event, **kwargs):
        nonlocal instant
        result = await independent(event, **kwargs)
        if event.outcome == "intent":
            instant += timedelta(days=1)
        return result

    monkeypatch.setattr(recorder, "_independent", advance_after_intent)
    calls = []

    async def handler(_):
        calls.append("effect")

    relay = await relay_for(store, factory, handler)
    await relay.run_once()
    await relay.run_once()
    assert calls == []
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "unknown"]


async def test_handler_cancel_message_is_redacted_and_never_replayed(relational_backend: RelationalBackend, caplog):
    _, journal, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])
    calls = []

    async def handler(_):
        calls.append("effect")
        raise asyncio.CancelledError(PRIVATE)

    relay = await relay_for(store, factory, handler, strategy=ErrorStrategy.RETRY)
    await relay.run_once()
    await relay.run_once()
    assert calls == ["effect"]
    assert PRIVATE not in caplog.text
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "unknown"]


@pytest.mark.parametrize("mode,exit_code", [("before_intent", 71), ("after_intent", 72), ("after_io", 73)])
async def test_process_death_recovery_fences_any_committed_intent(
    relational_backend: RelationalBackend,
    tmp_path,
    monkeypatch,
    mode,
    exit_code,
):
    _, journal, store, _, factory = await setup(relational_backend)
    await store.append(envelope(), groups=["worker"])
    marker, config = tmp_path / "external-effect.txt", tmp_path / "crash-fixture.json"
    config.write_text(json.dumps({"url": relational_backend.url, "mode": mode, "marker": str(marker)}))
    config.chmod(0o600)
    child = subprocess.run(
        [sys.executable, "-I", str(Path(__file__).parents[1] / "support/eda_audit_crash.py"), str(config)],
        capture_output=True,
        timeout=15,
        env={"PATH": os.defpath, "LANG": "C"},
    )
    assert child.returncode == exit_code, "Owned crash fixture failed before its expected boundary"
    future = datetime.now(UTC) + timedelta(days=2)
    monkeypatch.setattr(store, "_clock", lambda: future)
    calls = []

    async def handler(_):
        calls.append("effect")

    handler.__module__, handler.__qualname__ = "owned_eda_fixture", "handler"
    reopened = await relay_for(store, factory, handler)
    await reopened.run_once()
    assert calls == (["effect"] if mode == "before_intent" else [])
    assert marker.read_text() == "effect\n" if mode == "after_io" else not marker.exists()
    outcomes = [entry.event.outcome for entry in await journal.read(SCOPE)]
    assert outcomes == (["admitted", "intent", "observed"] if mode == "before_intent" else ["admitted", "intent"])
    assert not await store.pending("worker")


@configuration
class NativeEdaAuditFixture:
    @bean
    def trusted_audit_resolver(self) -> AuditContextResolver:
        class Resolver:
            async def resolve(self, operation):
                return AuditContext(
                    scope=SCOPE,
                    actor=AuditActor(kind="service", subject_id="verified-host"),
                    resource=AuditResource(kind="delivery", resource_id=operation.resource_key or "fixture"),
                )

        return Resolver()

    @bean
    def fixture_publisher(self, config: Config, datasources: DataSourceRegistry) -> EventPublisher:
        from pyfly.eda.adapters.database import DatabaseEventBus
        from pyfly.eda.adapters.memory import InMemoryEventBus
        from pyfly.eda.outbox_forwarding import TransactionalEventPublisher

        if config.get("fixture.adapter") == "database":
            return DatabaseEventBus(datasource=datasources.primary, group="worker", notify=False, poll_interval=3600)
        return TransactionalEventPublisher(
            InMemoryEventBus(),
            SqlOutboxStore(datasources.primary),
            name="fixture",
            poll_interval=3600,
        )


@pytest.mark.parametrize("adapter", ["database", "forwarding"])
async def test_native_di_binds_owned_store_and_relay_hooks(relational_backend: RelationalBackend, adapter):
    context = ApplicationContext(
        Config(
            {
                "fixture": {"adapter": adapter},
                "pyfly": {
                    "data": {"relational": {"enabled": True, "url": relational_backend.url, "ddl-auto": "create"}},
                    "audit": {"enabled": True, "recording": {"enabled": True, "modules": ["eda"]}},
                },
            }
        ),
        auto_configurations=[
            DataSourceAutoConfiguration,
            RelationalAutoConfiguration,
            AuditAutoConfiguration,
            AuditRecordingAutoConfiguration,
        ],
    )
    context.register_bean(NativeEdaAuditFixture)
    await context.start()
    try:
        publisher = context.get_bean(EventPublisher)
        await publisher.relay.stop()
        calls = []

        async def handler(_):
            calls.append("effect")

        publisher.subscribe("fixture.changed", handler)
        await publisher.publish("fixture", "fixture.changed", {"private": PRIVATE})
        await publisher.relay.run_once()
        journal = context.get_bean(AuditRecorder).journal
        assert calls == ["effect"]
        assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "observed"]
        assert PRIVATE not in (await journal.export(SCOPE, through_sequence=3)).decode()
    finally:
        await context.stop()


async def test_separate_native_outbox_tables_have_independent_admission_identity(relational_backend: RelationalBackend):
    engine, journal, one, _, factory = await setup(relational_backend)
    two = SqlOutboxStore(engine, tables=OutboxTables.named("eda_independent"))
    bind(two, factory)
    await two.start()
    calls = []

    async def handler(_):
        calls.append("effect")

    for store in (one, two):
        await store.append(envelope(), groups=["worker"])
        relay = await relay_for(store, factory, handler)
        await relay.run_once()
    assert calls == ["effect", "effect"]
    intents = [entry for entry in await journal.read(SCOPE) if entry.event.outcome == "intent"]
    assert len({entry.event.event_id for entry in intents}) == 2


async def test_unknown_settlement_failure_is_safe_and_restart_cannot_repeat_effect(
    relational_backend: RelationalBackend,
    monkeypatch,
):
    _, journal, store, _, factory = await setup(relational_backend)
    instant = datetime.now(UTC)
    monkeypatch.setattr(store, "_clock", lambda: instant)
    await store.append(envelope(), groups=["worker"])
    calls = []

    async def handler(_):
        calls.append("effect")
        raise RuntimeError(PRIVATE)

    settle = store.settle

    async def unavailable(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(store, "settle", unavailable)
    relay = await relay_for(store, factory, handler)
    with pytest.raises(AuditError) as failure:
        await relay.run_once()
    assert PRIVATE not in str(failure.value)
    monkeypatch.setattr(store, "settle", settle)
    instant += timedelta(days=1)
    reopened = await relay_for(store, factory, handler)
    await reopened.run_once()
    assert calls == ["effect"]
    assert [entry.event.outcome for entry in await journal.read(SCOPE)] == ["admitted", "intent", "unknown"]
    assert not await store.pending("worker")
