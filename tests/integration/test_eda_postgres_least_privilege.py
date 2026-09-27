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
"""A serving process runs the Postgres bus without any right to create schema.

The contract, written down (docs/modules/events.md, "Postgres: what privileges a serving process actually
needs"): once the outbox tables exist, a serving role needs ``USAGE`` on the schema and ``SELECT``, ``INSERT``,
``UPDATE`` and ``DELETE`` on the four outbox tables, nothing more. ``DELETE`` is new with the transactional
outbox (C146): a handled delivery row is deleted, and retention deletes the events every group handled. The
identity column of ``pyfly_outbox_events`` needs no grant on its sequence.

The adapter used to replay ``CREATE TABLE IF NOT EXISTS`` at every boot, and Postgres checks ``CREATE`` on the
schema *before* it checks ``IF NOT EXISTS``. Gated by ``@requires_docker``; collected only under
``-m integration``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from pyfly.eda.adapters.postgres import PostgresEventBus
from pyfly.eda.outbox import Retention
from pyfly.eda.types import EventEnvelope
from tests.support.backend_matrix import PG, RelationalBackend

pytestmark = pytest.mark.backends(PG)

SERVING_PASSWORD = "serving"  # noqa: S105 — throwaway role inside a disposable container
TABLES = ("pyfly_outbox_events", "pyfly_outbox_deliveries", "pyfly_outbox_consumers", "pyfly_outbox_dead_letters")


@pytest.fixture
async def least_privilege_url(relational_backend: RelationalBackend) -> AsyncIterator[str]:
    """Create the outbox tables as their owner, and a role that cannot create schema.

    The role gets exactly what the documentation lists: USAGE on the schema, and SELECT, INSERT, UPDATE and
    DELETE on the four tables. It is granted no CREATE on the schema and owns nothing.
    """
    owner = PostgresEventBus(dsn=relational_backend.url, group="owner")
    await owner.start()  # the owner creates the schema, as a migration or a first boot would
    await owner.stop()

    role = f"pyfly_serving_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(relational_backend.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{SERVING_PASSWORD}'"))
            # Explicit, so the test does not rely on PostgreSQL 15+ having taken CREATE on public away from
            # PUBLIC by default.
            await conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
            await conn.execute(text(f"REVOKE CREATE ON SCHEMA public FROM {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {role}"))
        yield (
            make_url(relational_backend.url)
            .set(username=role, password=SERVING_PASSWORD)
            .render_as_string(hide_password=False)
        )
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"REASSIGN OWNED BY {role} TO CURRENT_USER"))
            await conn.execute(text(f"DROP OWNED BY {role}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


async def test_the_role_cannot_create_schema(least_privilege_url: str) -> None:
    """The mechanism, pinned: an existing table does not save a role without CREATE on the schema."""
    engine = create_async_engine(least_privilege_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conn.execute(text("CREATE TABLE IF NOT EXISTS pyfly_outbox_events (id BIGINT)"))
    finally:
        await engine.dispose()


async def test_a_serving_role_boots_round_trips_and_prunes(least_privilege_url: str) -> None:
    """start() succeeds, the bus publishes and consumes, and retention deletes what was handled."""
    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    group = f"least-privilege-{uuid.uuid4().hex[:8]}"
    bus = PostgresEventBus(dsn=least_privilege_url, destinations=["pyfly.events"], group=group)
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
        pruned = await bus.outbox.prune(Retention(delivered=timedelta(0)))
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"
    assert pruned.delivered == 1


async def test_a_role_without_delete_cannot_settle_deliveries(
    relational_backend: RelationalBackend, least_privilege_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """What the grant is for: the earlier contract (SELECT, INSERT, UPDATE) cannot settle a delivery."""
    role = make_url(least_privilege_url).username
    admin = create_async_engine(relational_backend.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f"REVOKE DELETE ON {', '.join(TABLES)} FROM {role}"))
    finally:
        await admin.dispose()

    handled: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        handled.append(envelope.event_type)

    bus = PostgresEventBus(dsn=least_privilege_url, group="no-delete", poll_interval_s=0.2)
    bus.subscribe("*", handler)
    try:
        await bus.start()
        await bus.publish("d", "e", {})
        for _ in range(100):
            if bus.relay.counters.failed_rounds:
                break
            await asyncio.sleep(0.05)
        assert handled  # the handler ran; settling the delivery is what the role may not do
        failures = [record for record in caplog.records if record.getMessage() == "outbox_relay_round_failed"]
        assert failures and failures[0].exc_info is not None
        assert "permission denied" in str(failures[0].exc_info[1])
    finally:
        await bus.stop()
