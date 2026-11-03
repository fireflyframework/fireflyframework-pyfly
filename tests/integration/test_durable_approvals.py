# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Native PostgreSQL CAS via the existing pg_url fixture; no DROP or reset."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from pyfly.agentic.pipeline.durable import DurableConflict, DurableRunKey
from pyfly.agentic.pipeline.durable.sql import SqlDurableRunStore
from pyfly.audit import AuditJournal
from pyfly.audit.auto_configuration import AuditAutoConfiguration
from pyfly.audit.models import AuditScope
from pyfly.context import ApplicationContext
from pyfly.core import Config
from pyfly.data.relational.auto_configuration import DataSourceAutoConfiguration, RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy import SessionProvider, SqlAlchemyTransactionManager
from pyfly.data.transaction import UnexpectedRollbackError
from tests.agentic.unit.pipeline.test_durable_sql import AUTHOR, BINDING, INPUT, REVIEWER, record

pytestmark = [pytest.mark.integration, pytest.mark.pg]


@asynccontextmanager
async def opened(url, *, create):
    context = ApplicationContext(
        Config(
            {
                "pyfly": {
                    "audit": {"enabled": True},
                    "data": {"relational": {"enabled": True, "url": url, "ddl-auto": "create" if create else "none"}},
                }
            }
        ),
        auto_configurations=[DataSourceAutoConfiguration, RelationalAutoConfiguration, AuditAutoConfiguration],
    )
    await context.start()
    try:
        sessions = context.get_bean(SessionProvider)
        journal = context.get_bean(AuditJournal)
        store = SqlDurableRunStore(
            context.get_bean(SqlAlchemyTransactionManager), sessions, journal, create_tables=create
        )
        await store.start()
        try:
            yield store, sessions, journal
        finally:
            await store.stop()
    finally:
        await context.stop()


@pytest.fixture
async def pair(pg_url):
    url = pg_url
    key = DurableRunKey(
        scope=AuditScope(tenant_id="durable-" + uuid4().hex, workspace_id="fixture", project_id="approval"),
        run_id="run",
    )
    async with opened(url, create=True) as first, opened(url, create=False) as second:
        await first[0].create(key, BINDING, INPUT, actor=AUTHOR)
        lease = await first[0].claim(key, BINDING, owner="fixture-worker", expected_generation=1)
        await first[0].checkpoint(lease, record(1))
        waiting = await first[0].checkpoint(lease, record(2, paused=True))
        yield first, second, key, waiting


def request(waiting, *, decision="approve"):
    return {
        "task_id": waiting.task.task_id,
        "expected_generation": 1,
        "decision": decision,
        "actor": REVIEWER,
        "idempotency_key": "request-one",
        "policy_digest": "c" * 64,
    }


def barrier_reads(monkeypatch, stores):
    ready = asyncio.Event()
    arrived = 0
    for store in stores:
        original = store._load

        async def loaded(session, key, binding, load=original):
            nonlocal arrived
            value = await load(session, key, binding)
            if value.status == "waiting_approval":
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
            return value

        monkeypatch.setattr(store, "_load", loaded)


async def test_competing_different_decisions_have_one_cas_and_one_native_fact(pair, monkeypatch):
    first, second, key, waiting = pair
    barrier_reads(monkeypatch, [first[0], second[0]])
    outcomes = await asyncio.gather(
        first[0].decide(key, BINDING, **request(waiting)),
        second[0].decide(key, BINDING, **request(waiting, decision="reject")),
        return_exceptions=True,
    )
    assert len([v for v in outcomes if isinstance(v, DurableConflict)]) == 1
    assert len([v for v in outcomes if not isinstance(v, BaseException)]) == 1
    current = await first[0].load(key, BINDING)
    assert current.version == waiting.version + 1
    facts = await first[2].read(key.scope)
    assert len(facts) == current.version
    assert len([e for e in await first[0].events(key, BINDING) if e.kind in {"approved", "rejected"}]) == 1


async def test_same_key_conflict_or_lost_response_retries_return_exact_committed_decision(pair, monkeypatch):
    first, second, key, waiting = pair
    barrier_reads(monkeypatch, [first[0], second[0]])
    outcomes = await asyncio.gather(
        first[0].decide(key, BINDING, **request(waiting)),
        second[0].decide(key, BINDING, **request(waiting)),
        return_exceptions=True,
    )
    assert len([v for v in outcomes if isinstance(v, DurableConflict)]) == 1
    winner = next(v for v in outcomes if not isinstance(v, BaseException))
    head = await first[2].head(key.scope)
    for store in (first[0], second[0]):
        assert await store.decide(key, BINDING, **request(waiting)) == winner
    assert await first[2].head(key.scope) == head
    assert len(await first[0].events(key, BINDING)) == winner.version


async def test_audit_write_then_failure_rolls_back_decision_in_shared_primary_unit(pair, monkeypatch):
    first, _, key, waiting = pair
    store, sessions, journal = first
    head = await journal.head(key.scope)
    append = journal.append

    async def append_then_fail(event):
        await append(event)
        raise RuntimeError("private-fixture-fault")

    monkeypatch.setattr(journal, "append", append_then_fail)
    with pytest.raises(UnexpectedRollbackError) as error:
        async with sessions.unit():
            with pytest.raises(RuntimeError):
                await store.decide(key, BINDING, **request(waiting))
    assert "private-fixture-fault" not in str(error.value)
    assert await store.load(key, BINDING) == waiting
    assert await journal.head(key.scope) == head
    assert len(await store.events(key, BINDING)) == waiting.version
