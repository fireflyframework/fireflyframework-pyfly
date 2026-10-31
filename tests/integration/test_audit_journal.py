# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native journal contract on disposable file SQLite and PostgreSQL databases."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import Column, MetaData, String, Table, func, insert, select, text

from pyfly.audit import (
    AuditActor,
    AuditConflict,
    AuditError,
    AuditEvent,
    AuditIntegrityError,
    AuditResource,
    AuditScope,
)
from pyfly.audit.sql import SqlAuditJournal
from pyfly.data.relational.framework_schema import audit_entries, audit_heads
from pyfly.data.transaction import infrastructure_unit
from tests.support.backend_matrix import RelationalBackend

pytestmark = pytest.mark.backends("sqlite-file", "pg")
SCOPE = AuditScope(tenant_id="local", workspace_id="studio", project_id="default")
orders = Table("audit_test_orders", MetaData(), Column("id", String(64), primary_key=True))


def event(event_id: str = "event-1", **values: object) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        event_type="run.admitted",
        scope=SCOPE,
        actor=AuditActor(kind="human", subject_id="user-1"),
        resource=AuditResource(kind="run", resource_id="run-1", revision=1),
        occurred_at=datetime(2026, 10, 31, tzinfo=UTC),
        outcome="admitted",
        **values,
    )


async def journal(backend: RelationalBackend) -> SqlAuditJournal:
    instance = SqlAuditJournal(backend.create_engine())
    await instance.start()
    return instance


async def test_business_journal_head_and_outbox_rollback_together(relational_backend: RelationalBackend) -> None:
    instance = await journal(relational_backend)
    async with instance.engine.begin() as conn:
        await conn.run_sync(orders.create)
    with pytest.raises(RuntimeError, match="abort"):
        async with infrastructure_unit(instance.engine) as session:
            await session.execute(insert(orders).values(id="order-1"))
            await instance.append(event())
            raise RuntimeError("abort")
    async with instance.engine.connect() as conn:
        for table in (orders, audit_entries, audit_heads, instance.outbox.tables.events):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
    async with infrastructure_unit(instance.engine) as session:
        await session.execute(insert(orders).values(id="order-1"))
        first = await instance.append(event())
    assert first.sequence == 1
    assert (await instance.read(SCOPE))[0] == first
    deliveries = await instance.outbox.pending("pyfly.audit.export")
    assert len(deliveries) == 1
    assert deliveries[0].envelope.payload == {
        "journal_id": "default",
        "chain_id": first.chain_id,
        "sequence": 1,
        "entry_hash": first.entry_hash,
    }


async def test_append_requires_active_write_unit_and_idempotency_has_no_second_delivery(
    relational_backend: RelationalBackend,
) -> None:
    instance = await journal(relational_backend)
    with pytest.raises(RuntimeError, match="active"):
        await instance.append(event())
    async with infrastructure_unit(instance.engine, read_only=True):
        with pytest.raises(RuntimeError, match="write"):
            await instance.append(event())
    async with infrastructure_unit(instance.engine):
        first = await instance.append(event())
        assert await instance.append(event()) == first
    assert len(await instance.outbox.pending("pyfly.audit.export")) == 1
    with pytest.raises(AuditConflict):
        async with infrastructure_unit(instance.engine):
            await instance.append(event().model_copy(update={"outcome": "denied"}))
    assert len(await instance.read(SCOPE)) == 1


async def test_two_connections_and_inherited_tasks_do_not_fork_chain(relational_backend: RelationalBackend) -> None:
    instance = await journal(relational_backend)
    other = await journal(relational_backend)

    async def append(index: int) -> None:
        selected = instance if index % 2 else other
        async with infrastructure_unit(selected.engine):
            await selected.append(event(f"event-{index}"))

    await asyncio.gather(*(append(index) for index in range(12)))
    async with infrastructure_unit(instance.engine):
        await asyncio.gather(*(instance.append(event(f"child-{index}")) for index in range(6)))
    entries = await instance.read(SCOPE)
    assert [item.sequence for item in entries] == list(range(1, 19))
    assert len({item.event.event_id for item in entries}) == 18
    assert (await instance.verify(SCOPE)).head == entries[-1].head
    assert (await instance.read(SCOPE, after_sequence=5, limit=2)) == entries[5:7]


async def test_restart_export_scope_isolation_and_trusted_head(relational_backend: RelationalBackend) -> None:
    instance = await journal(relational_backend)
    async with infrastructure_unit(instance.engine):
        for index in range(3):
            await instance.append(event(f"event-{index}"))
    await instance.stop()
    reopened = await journal(relational_backend)
    data = await reopened.export(SCOPE, through_sequence=3)
    assert len(data.splitlines()) == 3
    assert [json.loads(line)["sequence"] for line in data.splitlines()] == [1, 2, 3]
    assert await reopened.export(SCOPE, through_sequence=3) == data
    head = (await reopened.verify(SCOPE)).head
    assert not (await reopened.verify(SCOPE)).anchored
    assert (await reopened.verify(SCOPE, trusted_head=head)).anchored
    assert not await reopened.read(SCOPE.model_copy(update={"project_id": "other"}))
    with pytest.raises(AuditIntegrityError):
        await reopened.verify(SCOPE.model_copy(update={"project_id": "other"}), trusted_head=head)


@pytest.mark.parametrize("mutation", ["UPDATE pyfly_audit_entries SET payload='{}'", "DELETE FROM pyfly_audit_entries"])
async def test_database_rejects_mutation(relational_backend: RelationalBackend, mutation: str) -> None:
    instance = await journal(relational_backend)
    async with infrastructure_unit(instance.engine):
        await instance.append(event())
    with pytest.raises(Exception, match="append.only"):
        async with instance.engine.begin() as conn:
            await conn.execute(text(mutation))
    assert (await instance.verify(SCOPE)).head.sequence == 1


async def test_export_fails_when_privileged_owner_changes_payload(relational_backend: RelationalBackend) -> None:
    instance = await journal(relational_backend)
    async with infrastructure_unit(instance.engine):
        await instance.append(event())
    async with instance.engine.begin() as conn:
        if relational_backend.is_embedded:
            await conn.execute(text("DROP TRIGGER pyfly_audit_entries_no_update"))
        else:
            await conn.execute(text("ALTER TABLE pyfly_audit_entries DISABLE TRIGGER USER"))
        await conn.execute(text("UPDATE pyfly_audit_entries SET payload='{}'"))
    with pytest.raises(AuditIntegrityError):
        await instance.export(SCOPE, through_sequence=1)


async def test_caught_conflict_still_rolls_back_business_change(relational_backend: RelationalBackend) -> None:
    from pyfly.data.transaction import UnexpectedRollbackError

    instance = await journal(relational_backend)
    async with instance.engine.begin() as conn:
        await conn.run_sync(orders.create)
    async with infrastructure_unit(instance.engine):
        await instance.append(event())
    with pytest.raises(UnexpectedRollbackError):
        async with infrastructure_unit(instance.engine) as session:
            await session.execute(insert(orders).values(id="must-rollback"))
            with pytest.raises(AuditConflict):
                await instance.append(event().model_copy(update={"outcome": "denied"}))
    async with instance.engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(orders)) == 0


async def test_caught_invalid_facts_still_roll_back_business_change(relational_backend: RelationalBackend) -> None:
    from pydantic import ValidationError

    from pyfly.data.transaction import UnexpectedRollbackError

    instance = await journal(relational_backend)
    async with instance.engine.begin() as conn:
        await conn.run_sync(orders.create)
    with pytest.raises(UnexpectedRollbackError):
        async with infrastructure_unit(instance.engine) as session:
            await session.execute(insert(orders).values(id="must-rollback"))
            with pytest.raises(ValidationError):
                await instance.append(event().model_copy(update={"outcome": "SECRET_CANARY"}))
    assert not await instance.read(SCOPE)


@pytest.mark.parametrize("failed_table", ["pyfly_audit_entries", "pyfly_outbox_events"])
async def test_storage_failure_rolls_back_journal_and_business_when_caught(
    relational_backend: RelationalBackend,
    failed_table: str,
) -> None:
    from pyfly.data.transaction import UnexpectedRollbackError

    instance = await journal(relational_backend)
    async with instance.engine.begin() as conn:
        await conn.run_sync(orders.create)
        if relational_backend.is_embedded:
            statement = (
                f"CREATE TRIGGER audit_test_outbox_failure BEFORE INSERT ON {failed_table} "
                "BEGIN SELECT RAISE(ABORT, 'outbox unavailable'); END"
            )
            await conn.execute(text(statement))
        else:
            await conn.execute(
                text(
                    "CREATE FUNCTION audit_test_outbox_failure() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'outbox unavailable'; END; $$"
                )
            )
            await conn.execute(
                text(
                    f"CREATE TRIGGER audit_test_outbox_failure BEFORE INSERT ON {failed_table} "
                    "FOR EACH ROW EXECUTE FUNCTION audit_test_outbox_failure()"
                )
            )
    with pytest.raises(UnexpectedRollbackError) as rolled_back:
        async with infrastructure_unit(instance.engine) as session:
            await session.execute(insert(orders).values(id="must-rollback"))
            with pytest.raises(AuditError, match="Audit journal append failed") as failed:
                await instance.append(event())
            assert "event-1" not in str(failed.value)
            assert "outbox unavailable" not in str(failed.value)
    assert "event-1" not in str(rolled_back.value)
    assert "outbox unavailable" not in str(rolled_back.value)
    async with instance.engine.connect() as conn:
        for table in (orders, audit_entries, audit_heads):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0


async def test_external_head_detects_privileged_suffix_truncation(relational_backend: RelationalBackend) -> None:
    instance = await journal(relational_backend)
    async with infrastructure_unit(instance.engine):
        first = await instance.append(event("first"))
        last = await instance.append(event("last"))
    async with instance.engine.begin() as conn:
        if relational_backend.is_embedded:
            await conn.execute(text("DROP TRIGGER pyfly_audit_entries_no_delete"))
        else:
            await conn.execute(text("ALTER TABLE pyfly_audit_entries DISABLE TRIGGER USER"))
        await conn.execute(text("DELETE FROM pyfly_audit_entries WHERE sequence=2"))
        await conn.execute(
            text("UPDATE pyfly_audit_heads SET sequence=1, entry_hash=:hash"), {"hash": first.entry_hash}
        )
    assert not (await instance.verify(SCOPE)).anchored  # valid local prefix alone cannot establish completeness
    with pytest.raises(AuditIntegrityError):
        await instance.verify(SCOPE, trusted_head=last.head)


async def test_verify_captures_prefix_without_rejecting_concurrent_append(
    relational_backend: RelationalBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyfly.data.relational.sqlalchemy.session import UnitSession

    instance = await journal(relational_backend)
    other = await journal(relational_backend)
    if relational_backend.is_embedded:
        async with instance.engine.connect() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
    async with infrastructure_unit(instance.engine):
        first = await instance.append(event("first"))
    captured, resume = asyncio.Event(), asyncio.Event()
    execute = UnitSession.execute
    reader = None

    async def pause_after_head(self, statement, *args, **kwargs):
        result = await execute(self, statement, *args, **kwargs)
        if asyncio.current_task() is reader and "pyfly_audit_heads" in str(statement) and not captured.is_set():
            captured.set()
            await resume.wait()
        return result

    monkeypatch.setattr(UnitSession, "execute", pause_after_head)
    reader = asyncio.create_task(instance.verify(SCOPE))
    await asyncio.wait_for(captured.wait(), timeout=5)
    try:
        async with infrastructure_unit(other.engine):
            await other.append(event("concurrent"))
    finally:
        resume.set()
    result = await reader
    assert result.head == first.head
    assert (await instance.verify(SCOPE)).head.sequence == 2


async def test_independent_processes_and_crash_recovery_preserve_committed_history(
    relational_backend: RelationalBackend,
) -> None:
    import os
    import sys

    instance = await journal(relational_backend)
    program = """
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from pyfly.audit.sql import SqlAuditJournal
from pyfly.data.transaction import infrastructure_unit
from tests.integration.test_audit_journal import event
async def main():
    engine = create_async_engine(os.environ["PYFLY_AUDIT_TEST_URL"])
    journal = SqlAuditJournal(engine, create_tables=False)
    await journal.start()
    for index in range(4):
        async with infrastructure_unit(engine):
            await journal.append(event(os.environ["PYFLY_AUDIT_TEST_PREFIX"] + str(index)))
            if os.environ.get("PYFLY_AUDIT_TEST_CRASH") == "1":
                os._exit(0)
    await journal.stop()
    await engine.dispose()
asyncio.run(main())
"""

    async def process(prefix: str, *, crash: bool = False) -> None:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PYFLY_AUDIT_TEST_URL": relational_backend.url,
                "PYFLY_AUDIT_TEST_PREFIX": prefix,
                "PYFLY_AUDIT_TEST_CRASH": "1" if crash else "0",
            },
        )
        _, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
        assert child.returncode == 0, stderr.decode()

    await asyncio.gather(*(process(f"process-{index}-") for index in range(4)))
    assert (await instance.verify(SCOPE)).head.sequence == 16
    await process("uncommitted-", crash=True)
    assert (await instance.verify(SCOPE)).head.sequence == 16
    await process("reopened-")
    assert (await instance.verify(SCOPE)).head.sequence == 20
    assert len(await instance.outbox.pending("pyfly.audit.export")) == 20


async def test_best_effort_telemetry_never_exposes_reference_values_or_breaks_journal(
    relational_backend: RelationalBackend,
) -> None:
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    meter = MeterProvider(metric_readers=[reader])
    instance = SqlAuditJournal(relational_backend.create_engine(), tracer_provider=tracer, meter_provider=meter)
    await instance.start()
    try:
        async with infrastructure_unit(instance.engine):
            await instance.append(event())
        await instance.verify(SCOPE)
        with pytest.raises(AuditConflict):
            async with infrastructure_unit(instance.engine):
                await instance.append(event().model_copy(update={"outcome": "denied"}))
        spans = exporter.get_finished_spans()
        assert [span.name for span in spans] == ["pyfly.audit.append", "pyfly.audit.verify", "pyfly.audit.append"]
        for span in spans:
            assert set(span.attributes) == {"audit.operation", "audit.outcome"}
            assert not span.events
        data = reader.get_metrics_data()
        points = data.resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
        assert sum(point.value for point in points) == 3
        assert all(set(point.attributes) == {"audit.operation", "audit.outcome"} for point in points)
    finally:
        tracer.shutdown()
        meter.shutdown()


async def test_failing_telemetry_provider_does_not_weaken_mandatory_append(
    relational_backend: RelationalBackend,
) -> None:
    class UnavailableTelemetry:
        def get_tracer(self, name):
            raise RuntimeError("telemetry unavailable")

    instance = SqlAuditJournal(relational_backend.create_engine(), tracer_provider=UnavailableTelemetry())
    await instance.start()
    async with infrastructure_unit(instance.engine):
        await instance.append(event())
    assert (await instance.verify(SCOPE)).head.sequence == 1


async def test_head_is_a_scoped_observation_without_scanning_history(relational_backend: RelationalBackend) -> None:
    from sqlalchemy import event as sqlalchemy_event

    instance = await journal(relational_backend)
    empty = await instance.head(SCOPE)
    assert empty.sequence == 0
    async with infrastructure_unit(instance.engine):
        first = await instance.append(event())
    statements = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sqlalchemy_event.listen(instance.engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await instance.head(SCOPE) == first.head
    finally:
        sqlalchemy_event.remove(instance.engine.sync_engine, "before_cursor_execute", observe)
    assert not any("pyfly_audit_entries" in statement for statement in statements)
    assert (await instance.head(SCOPE.model_copy(update={"project_id": "other"}))).sequence == 0


async def test_export_rejects_truncated_range_between_verification_and_materialization(
    relational_backend: RelationalBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = await journal(relational_backend)
    async with infrastructure_unit(instance.engine):
        await instance.append(event("first"))
        await instance.append(event("second"))
    waiting, resume = asyncio.Event(), asyncio.Event()
    original = instance.read

    async def read_after_barrier(*args, **kwargs):
        waiting.set()
        await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(instance, "read", read_after_barrier)
    task = asyncio.create_task(instance.export(SCOPE, through_sequence=2))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    try:
        async with instance.engine.begin() as conn:
            if relational_backend.is_embedded:
                await conn.execute(text("DROP TRIGGER pyfly_audit_entries_no_delete"))
            else:
                await conn.execute(text("ALTER TABLE pyfly_audit_entries DISABLE TRIGGER USER"))
            await conn.execute(text("DELETE FROM pyfly_audit_entries WHERE sequence=2"))
    finally:
        resume.set()
    with pytest.raises(AuditIntegrityError):
        await task
