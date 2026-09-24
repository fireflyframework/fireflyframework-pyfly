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
"""A serving process boots the Postgres bus without any right to create schema.

The finding, written down: `PostgresEventBus.start()` replayed the outbox DDL in
every process at every boot, and Postgres checks `CREATE` on the schema *before*
it checks `IF NOT EXISTS`. A deployment therefore had to keep a boot role that
owned the framework's tables and hand the serving role rights it never used.

These tests fail at `CREATE TABLE IF NOT EXISTS` on the pre-26.09.07 adapter and
pass on the gated one. Gated by ``@requires_docker``; collected only under
``-m integration``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest

from pyfly.eda.adapters.postgres import _DDL_OUTBOX, PostgresEventBus, _normalise_dsn
from pyfly.eda.types import EventEnvelope
from pyfly.testing import requires_docker

SERVING_PASSWORD = "serving"  # noqa: S105 — throwaway role inside a disposable container


def _with_credentials(dsn: str, user: str, password: str) -> str:
    """Rewrite *dsn*'s userinfo, keeping host, port and database."""
    parts = urlsplit(_normalise_dsn(dsn))
    host = parts.hostname or "localhost"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@pytest.fixture
async def least_privilege_dsn(pg_url: str) -> AsyncIterator[str]:
    """Create the outbox tables as their owner, and a role that cannot create schema.

    The role gets exactly what the adapter's real work needs: USAGE on the
    schema, SELECT/INSERT/UPDATE on the two tables, USAGE on the sequence. It is
    granted no CREATE on the schema and owns nothing.
    """
    asyncpg = pytest.importorskip("asyncpg")
    role = f"pyfly_serving_{uuid.uuid4().hex[:8]}"
    owner_dsn = _normalise_dsn(pg_url)

    conn = await asyncpg.connect(owner_dsn)
    try:
        # The owner creates the schema, exactly as a migration or a first boot would.
        await conn.execute(_DDL_OUTBOX)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pyfly_eda_offsets (
                consumer_group TEXT PRIMARY KEY,
                last_event_id  BIGINT NOT NULL DEFAULT 0,
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        await conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{SERVING_PASSWORD}'")
        # Explicit, so the test does not rely on PostgreSQL 15+ having taken
        # CREATE on public away from PUBLIC by default.
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        await conn.execute(f"REVOKE CREATE ON SCHEMA public FROM {role}")
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        await conn.execute(f"GRANT SELECT, INSERT, UPDATE ON pyfly_eda_outbox, pyfly_eda_offsets TO {role}")
        await conn.execute(f"GRANT USAGE ON SEQUENCE pyfly_eda_outbox_id_seq TO {role}")
    finally:
        await conn.close()

    try:
        yield _with_credentials(pg_url, role, SERVING_PASSWORD)
    finally:
        cleanup = await asyncpg.connect(owner_dsn)
        try:
            await cleanup.execute(f"REASSIGN OWNED BY {role} TO CURRENT_USER")
            await cleanup.execute(f"DROP OWNED BY {role}")
            await cleanup.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await cleanup.close()


@requires_docker
@pytest.mark.asyncio
async def test_the_ddl_the_adapter_used_to_replay_is_refused(least_privilege_dsn: str) -> None:
    """The mechanism, pinned: an existing table does not save a role without CREATE."""
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(least_privilege_dsn)
    try:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(_DDL_OUTBOX)
    finally:
        await conn.close()


@requires_docker
@pytest.mark.asyncio
async def test_a_serving_role_boots_and_round_trips(least_privilege_dsn: str) -> None:
    """start() succeeds, and the bus still publishes and consumes."""
    received: list[EventEnvelope] = []
    done = asyncio.Event()

    async def handler(envelope: EventEnvelope) -> None:
        received.append(envelope)
        done.set()

    group = f"least-privilege-{uuid.uuid4().hex[:8]}"
    bus = PostgresEventBus(dsn=least_privilege_dsn, destinations=["pyfly.events"], group=group)
    bus.subscribe("order.*", handler)
    try:
        await bus.start()
        await bus.publish("pyfly.events", "order.created", {"id": 1})
        await asyncio.wait_for(done.wait(), timeout=15)
    finally:
        await bus.stop()

    assert len(received) == 1
    assert received[0].event_type == "order.created"


@requires_docker
@pytest.mark.asyncio
async def test_a_first_boot_against_an_empty_database_still_creates_the_tables(pg_url: str) -> None:
    """The opt-out default has not cost the zero-configuration first run."""
    asyncpg = pytest.importorskip("asyncpg")
    owner_dsn = _normalise_dsn(pg_url)
    conn = await asyncpg.connect(owner_dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS pyfly_eda_outbox, pyfly_eda_offsets")
    finally:
        await conn.close()

    bus = PostgresEventBus(dsn=pg_url, group=f"first-boot-{uuid.uuid4().hex[:8]}")
    try:
        await bus.start()
    finally:
        await bus.stop()

    conn = await asyncpg.connect(owner_dsn)
    try:
        present = await conn.fetchval(
            "SELECT to_regclass('pyfly_eda_outbox') IS NOT NULL AND to_regclass('pyfly_eda_offsets') IS NOT NULL"
        )
    finally:
        await conn.close()
    assert present is True
