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
"""The SQL OAuth2 token store on every relational lane (WP10b: C016, C072, C073, C074, C153).

The store kept every record as a JSON blob under a key and ran each grant as separate autocommitted
check-then-act statements: one refresh token or code was redeemed N times concurrently, a rotation racing a
revocation wrote the revoked family back as active, a failure between "mark used" and "store the new token"
logged the client out as a thief, and the table (with a family blob that grew per rotation) was never purged.
Each grant is now one unit of work of conditional statements over typed columns.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util import await_only

from pyfly.data.relational.framework_schema import FrameworkSchemaError, oauth2_grants, oauth2_token_families
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from pyfly.kernel.exceptions import SecurityException
from pyfly.security.adapters.postgres_token_store import PostgresTokenStore
from pyfly.security.oauth2.authorization_server import REFRESH_TOKEN
from pyfly.testing import StatementCounter
from pyfly.testing.statement_counter import statement_verb
from tests.integration import _oauth2_grants as grants
from tests.integration._repository_harness import repository_datasources
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend

_WRITES = frozenset({"INSERT", "UPDATE", "DELETE"})


@contextlib.asynccontextmanager
async def _store(backend: RelationalBackend, **options: Any) -> Any:
    async with repository_datasources(backend) as datasources:
        store = PostgresTokenStore(datasources.registry.primary, **options)
        await store.start()
        yield store, datasources.engine


async def _rows(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


# ---------------------------------------------------------------------------------------------------------
# Single use under concurrency (C073, C074)
# ---------------------------------------------------------------------------------------------------------


async def test_one_refresh_token_is_redeemed_once(relational_backend: RelationalBackend) -> None:
    async with _store(relational_backend) as (store, _engine):
        await grants.one_refresh_token_is_redeemed_once(store)


async def test_one_authorization_code_is_redeemed_once(relational_backend: RelationalBackend) -> None:
    async with _store(relational_backend) as (store, _engine):
        await grants.one_code_is_redeemed_once(store)


async def test_one_pushed_request_uri_is_consumed_once(relational_backend: RelationalBackend) -> None:
    async with _store(relational_backend) as (store, _engine):
        await grants.one_pushed_request_is_consumed_once(store)


@pytest.mark.parametrize("scenario", grants.SEQUENTIAL_SCENARIOS, ids=lambda scenario: scenario.__name__)
async def test_grant_semantics(relational_backend: RelationalBackend, scenario: Any) -> None:
    async with _store(relational_backend) as (store, _engine):
        await scenario(store)


async def test_two_servers_on_one_database_redeem_a_code_once(relational_backend: RelationalBackend) -> None:
    """Two replicas (two engines, two stores) behind a load balancer honour one code once."""
    async with _store(relational_backend) as (store, _engine):
        other = PostgresTokenStore(relational_backend.create_engine())
        first, second = grants.server(store), grants.server(other)
        code = await grants.code_for(first)

        outcomes = await asyncio.gather(
            *(grants.attempt(grants.redeem(replica, code)) for replica in (first, second) * 5)
        )

        granted, _refused = grants.split(outcomes)
        assert len(granted) == 1


# ---------------------------------------------------------------------------------------------------------
# A rotation racing a revocation (C016)
# ---------------------------------------------------------------------------------------------------------

_PAUSED_TASK: ContextVar[bool] = ContextVar("wp10b_paused_task", default=False)


@contextlib.contextmanager
def _pause_before_write(engine: AsyncEngine, nth: int, paused: asyncio.Event, resume: asyncio.Event) -> Iterator[None]:
    """Hold the task that set ``_PAUSED_TASK`` just before its *nth* write statement, until *resume* is set."""
    writes = 0

    def before(_conn: Connection, _cursor: Any, statement: str, *_args: Any) -> None:
        nonlocal writes
        if not _PAUSED_TASK.get() or statement_verb(statement) not in _WRITES:
            return
        writes += 1
        if writes == nth:
            paused.set()
            await_only(resume.wait())

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@pytest.mark.parametrize("victim", ["replay", "revoke"])
async def test_a_rotation_racing_a_revocation_never_resurrects_the_family(
    relational_backend: RelationalBackend, victim: str
) -> None:
    """The thief rotates the current token R1 while the victim replays the rotated R0 (reuse detection) or
    an admin revokes R1 (RFC 7009). The thief is held after its first writes, before its last one; the
    revocation runs meanwhile. Whatever the order, the family ends revoked: the thief's new token is dead."""
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        r0 = await grants.issue(authorization_server)
        r1 = (await grants.refresh(authorization_server, r0))["refresh_token"]
        paused, resume = asyncio.Event(), asyncio.Event()

        async def thief() -> Any:
            _PAUSED_TASK.set(True)
            return await grants.attempt(grants.refresh(authorization_server, r1))

        async def revocation() -> Any:
            if victim == "replay":
                return await grants.attempt(grants.refresh(authorization_server, r0))
            await authorization_server.revoke(r1)
            return None

        with _pause_before_write(engine, 3, paused, resume):
            thief_task = asyncio.create_task(thief())
            await asyncio.wait_for(paused.wait(), 10)
            victim_task = asyncio.create_task(revocation())
            await asyncio.sleep(0.3)  # the revocation runs, or waits for the thief's locks
            resume.set()
            stolen, _ = await asyncio.gather(thief_task, victim_task)

        if isinstance(stolen, dict):
            r2 = stolen["refresh_token"]
            assert not await grants.is_active(authorization_server, r2), "the revoked family came back"
            again = await grants.attempt(grants.refresh(authorization_server, r2))
            assert isinstance(again, SecurityException) and again.code == "INVALID_GRANT"
        else:
            assert isinstance(stolen, SecurityException) and stolen.code == "INVALID_GRANT"
        assert not await grants.is_active(authorization_server, r1)


# ---------------------------------------------------------------------------------------------------------
# A failure inside a grant (C153)
# ---------------------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _fail_write(engine: AsyncEngine, nth: int) -> Iterator[list[str]]:
    """Make the *nth* write statement from now on fail once, as a dropped connection would."""
    failed: list[str] = []
    writes = 0

    def before(_conn: Connection, _cursor: Any, statement: str, *_args: Any) -> None:
        nonlocal writes
        if failed or statement_verb(statement) not in _WRITES:
            return
        writes += 1
        if writes == nth:
            failed.append(statement)
            raise OperationalError(statement, {}, ConnectionResetError("simulated failover"))

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield failed
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@pytest.mark.parametrize("nth", [2, 3])
async def test_a_refresh_that_fails_midway_leaves_the_token_redeemable(
    relational_backend: RelationalBackend, nth: int
) -> None:
    """A transient failure after "mark used" used to turn the client's retry into a false theft signal and a
    forced logout. The grant is one unit: it rolls back whole, and the retry succeeds."""
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        token = await grants.issue(authorization_server)

        with _fail_write(engine, nth) as failed, pytest.raises(OperationalError):
            await grants.refresh(authorization_server, token)
        assert failed

        assert await grants.is_active(authorization_server, token)
        retried = await grants.refresh(authorization_server, token)
        assert await grants.is_active(authorization_server, retried["refresh_token"])


@pytest.mark.parametrize("nth", [2, 3])
async def test_a_code_grant_that_fails_midway_leaves_no_orphan(relational_backend: RelationalBackend, nth: int) -> None:
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        code = await grants.code_for(authorization_server)

        with _fail_write(engine, nth) as failed, pytest.raises(OperationalError):
            await grants.redeem(authorization_server, code)
        assert failed

        tokens = await grants.redeem(authorization_server, code)
        assert await grants.is_active(authorization_server, tokens["refresh_token"])
        assert await _rows(engine, oauth2_token_families) == 1
        async with engine.connect() as connection:
            refresh_rows = await connection.execute(
                select(func.count()).select_from(oauth2_grants).where(oauth2_grants.c.kind == REFRESH_TOKEN)
            )
            assert refresh_rows.scalar_one() == 1


async def test_a_refresh_is_two_checkouts_and_one_writing_unit(relational_backend: RelationalBackend) -> None:
    """A refresh cost 6 checkouts and 3 commits: now a read and one unit of conditional writes."""
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        token = await grants.issue(authorization_server)
        checkouts: list[int] = []

        def checkout(*_args: Any) -> None:
            checkouts.append(1)

        event.listen(engine.sync_engine, "checkout", checkout)
        try:
            with StatementCounter(engine) as counter:
                await grants.refresh(authorization_server, token)
        finally:
            event.remove(engine.sync_engine, "checkout", checkout)

        assert len(checkouts) == 2
        assert [verb for verb in counter.verbs() if verb in _WRITES] == ["UPDATE", "UPDATE", "INSERT"]


@pytest.mark.backends(PG, MYSQL, MARIADB)
async def test_a_grant_is_not_undone_by_the_callers_rollback(relational_backend: RelationalBackend) -> None:
    """The reuse defense revokes the family even when the token endpoint is called inside a unit of work that
    rolls back (the SecurityException it raises rolls the caller back)."""
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        r0 = await grants.issue(authorization_server)
        r1 = (await grants.refresh(authorization_server, r0))["refresh_token"]
        template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

        with pytest.raises(SecurityException, match="reuse"):
            async with template.transaction():
                await grants.refresh(authorization_server, r0)

        assert not await grants.is_active(authorization_server, r1)


# ---------------------------------------------------------------------------------------------------------
# Typed columns, exact keys and purge (C072)
# ---------------------------------------------------------------------------------------------------------


async def test_the_tables_have_typed_columns_and_an_expiry_index(relational_backend: RelationalBackend) -> None:
    async with _store(relational_backend) as (_store_, engine):

        def shape(connection: Connection) -> tuple[set[str], set[str], set[str]]:
            inspector = inspect(connection)
            columns = {column["name"] for column in inspector.get_columns(oauth2_grants.name)}
            indexed = {
                name
                for index in inspector.get_indexes(oauth2_grants.name)
                for name in index["column_names"]
                if name is not None
            }
            families = {column["name"] for column in inspector.get_columns(oauth2_token_families.name)}
            return columns, indexed, families

        async with engine.connect() as connection:
            columns, indexed, families = await connection.run_sync(shape)

        assert {"token_id", "kind", "client_id", "family_id", "used", "expires_at", "data"} <= columns
        assert {"expires_at", "family_id"} <= indexed
        assert {"family_id", "client_id", "active", "expires_at"} <= families


async def test_token_ids_compare_exactly(relational_backend: RelationalBackend) -> None:
    """MySQL and MariaDB compare with a collation that ignores case: a token's other-case spelling must not be
    the token."""
    async with _store(relational_backend) as (store, _engine):
        authorization_server = grants.server(store)
        token = await grants.issue(authorization_server)
        other_case = token.swapcase()
        assert other_case != token

        assert await store.load(REFRESH_TOKEN, other_case) is None
        assert not await grants.is_active(authorization_server, other_case)
        assert await grants.is_active(authorization_server, token)


async def test_revoking_a_long_family_is_a_fixed_number_of_statements(relational_backend: RelationalBackend) -> None:
    """Revoking a family of 2000 rotations took 2005 statements and 2003 commits; it is now one UPDATE of the
    family and one DELETE of its tokens, whatever its length."""
    async with _store(relational_backend) as (store, engine):
        authorization_server = grants.server(store)
        token = await grants.issue(authorization_server)
        for _ in range(25):
            token = (await grants.refresh(authorization_server, token))["refresh_token"]

        with StatementCounter(engine) as counter:
            await authorization_server.revoke(token)

        writes = [verb for verb in counter.verbs() if verb in _WRITES]
        assert writes == ["UPDATE", "DELETE"]
        assert counter.commits == 1
        assert await _rows(engine, oauth2_token_families) == 1  # one row per family, not a growing blob


async def test_expired_records_are_purged(relational_backend: RelationalBackend) -> None:
    """Used and expired tokens, codes, pushed requests and families were kept forever. A purge deletes what
    expired more than the reuse-detection grace period ago, and keeps the rest."""
    now = [datetime.now(UTC)]
    async with _store(relational_backend, clock=lambda: now[0], purge_interval=None) as (store, engine):
        short = grants.server(store, refresh_token_ttl=60, auth_code_ttl=60)
        long = grants.server(store, refresh_token_ttl=86400)
        rotated = await grants.issue(short)
        await grants.refresh(short, rotated)
        await grants.code_for(short)
        await short.pushed_authorization_request("web", {"scope": "read"})
        kept = await grants.issue(long)
        assert await _rows(engine, oauth2_grants) == 5

        assert await store.purge_expired() == 0  # nothing expired yet

        now[0] = now[0] + timedelta(seconds=120) + store.purge_grace
        purged = await store.purge_expired()

        assert purged == 5  # two tokens, the code, the pushed request and the short family
        assert await _rows(engine, oauth2_grants) == 1
        assert await _rows(engine, oauth2_token_families) == 1
        assert await grants.is_active(long, kept)


async def test_writes_purge_expired_records_after_their_commit(relational_backend: RelationalBackend) -> None:
    now = [datetime.now(UTC)]
    async with _store(relational_backend, clock=lambda: now[0], purge_interval=timedelta(0)) as (store, engine):
        authorization_server = grants.server(store, auth_code_ttl=1)
        for _ in range(3):
            await grants.code_for(authorization_server)
        now[0] = now[0] + timedelta(seconds=5) + store.purge_grace

        await grants.issue(authorization_server)

        assert await _rows(engine, oauth2_grants) == 1


async def test_without_ddl_the_missing_tables_fail_fast(relational_backend: RelationalBackend) -> None:
    store = PostgresTokenStore(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="pyfly_oauth2_grants"):
        await store.start()


async def test_the_store_starts_idempotently_and_expiry_is_an_instant(relational_backend: RelationalBackend) -> None:
    async with _store(relational_backend) as (store, engine):
        await store.start()
        authorization_server = grants.server(store)
        token = await grants.issue(authorization_server)
        record = await store.load(REFRESH_TOKEN, token)
        assert record is not None
        assert abs(record.expires_at - (int(time.time()) + 86400)) <= 2
        async with engine.connect() as connection:
            stored = (
                await connection.execute(select(oauth2_grants.c.expires_at).where(oauth2_grants.c.token_id == token))
            ).scalar_one()
        assert stored.tzinfo is not None
        assert abs(stored.timestamp() - record.expires_at) < 1
