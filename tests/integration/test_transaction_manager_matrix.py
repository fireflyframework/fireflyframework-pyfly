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
"""The unit of work on every backend of the matrix (SQLite file, PostgreSQL, MySQL 8, MariaDB 11).

- The propagation x outcome matrix (WP01-02, WP01-04, WP01-10): every propagation as the inner call of a
  ``REQUIRED`` unit, when everything commits, when the outer unit fails afterwards, and when the inner
  call fails and the outer catches it. SQLite has one writer, so there a write that would wait for its
  own suspended outer unit fails fast instead.
- Isolation is applied and read back from the connection, and an unsupported level fails at begin
  (WP01-07).
- Read-only refuses writes on every backend; the dialect hint refuses raw SQL writes on PostgreSQL, MySQL
  and MariaDB (WP01-08).
- A read auto unit sends no ``BEGIN`` on PostgreSQL (seen through asyncpg's query logger, since the
  statement counter cannot see ``BEGIN`` there), unless an after-begin customizer needs a transaction;
  the customizer's transaction-local GUC is visible to the read (WP01-03).
- Retries around a transaction, in both decorator orders, never commit a failed attempt's writes and
  never retry an unknown commit outcome (WP01-17).
- PostgreSQL only: a cancel in the middle of a statement, two cancels during a rollback held back by a
  partitioned network, a commit whose backend is terminated in flight, and the statement timeout that
  cancels a unit's statement on the server (WP01-12, WP01-13).
- SQLite file and PostgreSQL: a client that disconnects from a Server-Sent Events stream served by a real
  uvicorn server while a unit is open (Starlette cancels the stream through an anyio scope, C062).
- Every backend: repositories reached through every holder shape write inside the one unit (C006), and
  ``gather()`` fan-out inside a unit is serialized without poisoning a pooled connection (C005).
- PostgreSQL: a single-statement ``infrastructure_unit()`` outside a transaction runs on ``AUTOCOMMIT``.

Every scenario ends with no pooled connection checked out and, on PostgreSQL, no backend idle in
transaction.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from sqlalchemy import Identity, Integer, String, event, insert, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.provider import Provider
from pyfly.container.stereotypes import component, repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data import Isolation, Propagation, transactional
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSource, DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transaction import (
    CommitOutcomeUnknownError,
    IllegalTransactionStateError,
    TransactionSynchronizationAdapter,
    TransactionTimedOutError,
    UnexpectedRollbackError,
    infrastructure_unit,
    register_synchronization,
)
from pyfly.resilience.retry import retry
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend
from tests.support.partition_proxy import PartitionProxy


class MxItem(Base):
    __tablename__ = "mx_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


@repository
class MxItemRepository(Repository[MxItem, int]):
    async def find_setting(self, name: str) -> str | None:
        """A read method that reports a transaction-local setting (PostgreSQL)."""
        return (await self._session.execute(text(f"SELECT current_setting('{name}', true)"))).scalar_one()

    async def find_and_rename(self, name: str) -> None:
        """Misnamed: a read-named method that issues a Core UPDATE (it runs in a read auto unit)."""
        await self._session.execute(update(MxItem).values(name=name))


class InnerFailure(Exception):
    pass


@service
class Inner:
    def __init__(self, items: MxItemRepository) -> None:
        self.items = items

    async def _write(self, fail: bool) -> None:
        await self.items.save(MxItem(name="inner"))
        if fail:
            raise InnerFailure("the inner call fails after writing")

    @transactional(propagation=Propagation.REQUIRED)
    async def required(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def requires_new(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.NESTED)
    async def nested(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.SUPPORTS)
    async def supports(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.NOT_SUPPORTED)
    async def not_supported(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.MANDATORY)
    async def mandatory(self, fail: bool) -> None:
        await self._write(fail)

    @transactional(propagation=Propagation.NEVER)
    async def never(self, fail: bool) -> None:
        await self._write(fail)


@service
class Outer:
    def __init__(self, items: MxItemRepository, inner: Inner) -> None:
        self.items = items
        self.inner = inner
        self.attempts = 0

    @transactional
    async def scenario(self, propagation: Propagation, *, inner_fails: bool, catch: bool, outer_fails: bool) -> None:
        await self.items.save(MxItem(name="outer-before"))
        call = getattr(self.inner, propagation.value.lower())
        if catch:
            with contextlib.suppress(InnerFailure, IllegalTransactionStateError):
                await call(inner_fails)
        else:
            await call(inner_fails)
        await self.items.save(MxItem(name="outer-after"))
        if outer_fails:
            raise ValueError("the outer unit fails afterwards")

    @transactional(isolation=Isolation.SERIALIZABLE)
    async def serializable_level(self, dialect: str) -> str:
        return await _isolation_level(self.items._session, dialect)

    @transactional(isolation=Isolation.READ_UNCOMMITTED)
    async def read_uncommitted_level(self, dialect: str) -> str:
        return await _isolation_level(self.items._session, dialect)

    @transactional(read_only=True)
    async def raw_write_in_read_only(self) -> None:
        await self.items._session.execute(text("INSERT INTO mx_item (name) VALUES ('raw')"))

    @transactional(read_only=True)
    async def orm_write_in_read_only(self) -> None:
        await self.items.save(MxItem(name="orm"))

    @transactional(read_only=True)
    async def core_write_in_read_only(self) -> None:
        await self.items._session.execute(insert(MxItem).values(name="core"))

    @retry(max_attempts=3, exceptions=(ConnectionError,))
    @transactional
    async def flaky_retry_outside(self) -> None:
        self.attempts += 1
        await self.items.save(MxItem(name=f"attempt-{self.attempts}"))
        if self.attempts == 1:
            raise ConnectionError("transient")

    @transactional
    @retry(max_attempts=3, exceptions=(ConnectionError,))
    async def flaky_retry_written_inside(self) -> None:
        self.attempts += 1
        await self.items.save(MxItem(name=f"attempt-{self.attempts}"))
        if self.attempts == 1:
            raise ConnectionError("transient")

    @transactional
    @retry(max_attempts=3, exceptions=(IntegrityError,))
    async def duplicate_then_fixed(self) -> None:
        self.attempts += 1
        await self.items.save(MxItem(name="taken" if self.attempts == 1 else "free"))

    @transactional
    async def place(self, name: str) -> None:
        await self.items.save(MxItem(name=name))

    @transactional
    async def sleep_in_database(self, seconds: float) -> None:
        await self.items.save(MxItem(name="sleeper"))
        await self.items._session.execute(text(f"SELECT pg_sleep({seconds})"))

    @transactional
    async def fail_while_partitioned(self, proxy: PartitionProxy, gate: Gate) -> None:
        register_synchronization(gate)
        await self.items.save(MxItem(name="doomed"))
        proxy.partition()
        raise ValueError("the rollback is held back by the partition")

    @transactional
    async def save_event(self, name: str) -> None:
        await self.items.save(MxItem(name=name))
        await asyncio.sleep(0.3)  # the unit is open when the client goes away

    @transactional(timeout=5)
    async def statement_timeout(self) -> str:
        return str((await self.items._session.execute(text("SHOW statement_timeout"))).scalar_one())

    @transactional(timeout=0.5)
    async def runaway(self) -> None:
        await self.items._session.execute(text("SELECT pg_sleep(5)"))

    @transactional
    async def commit_loses_its_backend(self, admin_url: str) -> None:
        self.attempts += 1
        pid = int((await self.items._session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        await self.items.save(MxItem(name="maybe-committed"))
        register_synchronization(_Terminate(admin_url, pid))


@service
class Holders:
    """Repositories reached through every shape the old structural patching missed (C006)."""

    def __init__(
        self,
        inner: Inner,
        repos: list[MxItemRepository],
        provider: Provider[MxItemRepository],
        session: AsyncSession,
    ) -> None:
        self.deep = {"level": [inner]}
        self.by_name = {"items": repos[0]}
        self.provider = provider
        self.session = session

    @transactional
    async def write_everywhere(self, *, fail: bool) -> None:
        await self.deep["level"][0].items.save(MxItem(name="deep"))
        await self.by_name["items"].save(MxItem(name="dict"))
        await self.provider.get().save(MxItem(name="provider"))
        self.session.add(MxItem(name="session"))
        await self.session.flush()
        if fail:
            raise ValueError("every shape rolls back together")

    @transactional
    async def fan_out(self) -> None:
        items = self.provider.get()
        await asyncio.gather(*(items.save(MxItem(name=f"child-{i}")) for i in range(8)))


class Gate(TransactionSynchronizationAdapter):
    def __init__(self) -> None:
        self.reached = asyncio.Event()

    async def before_completion(self) -> None:
        self.reached.set()


class _Terminate(TransactionSynchronizationAdapter):
    """Terminates the unit's own backend right before COMMIT: the commit fails in flight."""

    def __init__(self, admin_url: str, pid: int) -> None:
        self._admin_url = admin_url
        self._pid = pid

    async def before_commit(self, read_only: bool) -> None:
        admin = create_async_engine(self._admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": self._pid})
        finally:
            await admin.dispose()


@component
class TenantGuc:
    """The dworkers-style tenant GUC: transaction-local, so it needs a real transaction to be seen."""

    async def after_begin(self, connection: AsyncSession | AsyncConnection, datasource: DataSource) -> None:
        if datasource.capabilities.dialect == "postgresql":
            await connection.execute(text("SELECT set_config('app.tenant', 'acme', true)"))


async def _isolation_level(session: AsyncSession, dialect: str) -> str:
    if dialect == "postgresql":
        return str((await session.execute(text("SHOW transaction_isolation"))).scalar_one()).upper()
    if dialect in ("mysql", "mariadb"):
        variable = "@@transaction_isolation" if dialect == "mysql" else "@@tx_isolation"
        return str((await session.execute(text(f"SELECT {variable}"))).scalar_one()).replace("-", " ").upper()
    uncommitted = (await session.execute(text("PRAGMA read_uncommitted"))).scalar_one()
    return "READ UNCOMMITTED" if uncommitted else "SERIALIZABLE"


# ---------------------------------------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------------------------------------


class Matrix:
    def __init__(self, backend: RelationalBackend, ctx: ApplicationContext) -> None:
        self.backend = backend
        self.ctx = ctx

    @property
    def outer(self) -> Outer:
        return self.ctx.get_bean(Outer)

    @property
    def engine(self) -> AsyncEngine:
        return self.ctx.get_bean(DataSourceRegistry).primary.engine

    def checked_out(self) -> int:
        registry = self.ctx.get_bean(DataSourceRegistry)
        return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())

    async def committed(self) -> list[str]:
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return [r[0] for r in (await conn.execute(text("SELECT name FROM mx_item ORDER BY id"))).all()]
        finally:
            await engine.dispose()

    async def idle_in_transaction(self) -> int:
        if self.backend.dialect != "postgresql":
            return 0
        engine = create_async_engine(self.backend.url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                sql = (
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
                return int((await conn.execute(text(sql))).scalar_one())
        finally:
            await engine.dispose()

    async def assert_clean(self) -> None:
        assert self.checked_out() == 0
        assert await self.idle_in_transaction() == 0


async def _matrix(backend: RelationalBackend, *extra: type, overrides: dict[str, Any] | None = None) -> Matrix:
    await backend.create_tables(MxItem)
    ctx = ApplicationContext(backend.config(overrides))
    for bean in (RelationalAutoConfiguration, MxItemRepository, Inner, Outer, *extra):
        ctx.register_bean(bean)
    await ctx.start()
    return Matrix(backend, ctx)


@pytest.fixture
async def matrix(relational_backend: RelationalBackend) -> AsyncIterator[Matrix]:
    started = await _matrix(relational_backend)
    try:
        yield started
        await started.assert_clean()
    finally:
        await started.ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Propagation x outcome
# ---------------------------------------------------------------------------------------------------------

BOTH = ["outer-before", "inner", "outer-after"]
OUTER = ["outer-before", "outer-after"]

# propagation -> scenario -> (expected exception or None, committed rows) on a server backend.
EXPECTED: dict[Propagation, dict[str, tuple[type[BaseException] | None, list[str]]]] = {
    Propagation.REQUIRED: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, []),
        "caught_inner_failure": (UnexpectedRollbackError, []),
    },
    Propagation.MANDATORY: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, []),
        "caught_inner_failure": (UnexpectedRollbackError, []),
    },
    Propagation.SUPPORTS: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, []),
        "caught_inner_failure": (UnexpectedRollbackError, []),
    },
    Propagation.REQUIRES_NEW: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, ["inner"]),
        "caught_inner_failure": (None, OUTER),
    },
    Propagation.NESTED: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, []),
        "caught_inner_failure": (None, OUTER),
    },
    Propagation.NOT_SUPPORTED: {
        "commit": (None, BOTH),
        "outer_fails": (ValueError, ["inner"]),
        "caught_inner_failure": (None, BOTH),
    },
    Propagation.NEVER: {
        "commit": (IllegalTransactionStateError, []),
        "outer_fails": (IllegalTransactionStateError, []),
        "caught_inner_failure": (None, OUTER),
    },
}

# SQLite has one writer: a write that would wait for its own suspended outer unit fails fast.
EXPECTED_SQLITE: dict[Propagation, dict[str, tuple[type[BaseException] | None, list[str]]]] = {
    Propagation.REQUIRES_NEW: {
        "commit": (IllegalTransactionStateError, []),
        "outer_fails": (IllegalTransactionStateError, []),
        "caught_inner_failure": (None, OUTER),
    },
    Propagation.NOT_SUPPORTED: {
        "commit": (IllegalTransactionStateError, []),
        "outer_fails": (IllegalTransactionStateError, []),
        "caught_inner_failure": (None, OUTER),
    },
}

SCENARIOS = {
    "commit": {"inner_fails": False, "catch": False, "outer_fails": False},
    "outer_fails": {"inner_fails": False, "catch": False, "outer_fails": True},
    "caught_inner_failure": {"inner_fails": True, "catch": True, "outer_fails": False},
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
@pytest.mark.parametrize("propagation", list(Propagation), ids=lambda p: p.value)
async def test_propagation_outcome_matrix(matrix: Matrix, propagation: Propagation, scenario: str) -> None:
    expected_error, expected_rows = EXPECTED[propagation][scenario]
    if matrix.backend.is_embedded and propagation in EXPECTED_SQLITE:
        expected_error, expected_rows = EXPECTED_SQLITE[propagation][scenario]
    if expected_error is None:
        await matrix.outer.scenario(propagation, **SCENARIOS[scenario])
    else:
        with pytest.raises(expected_error):
            await matrix.outer.scenario(propagation, **SCENARIOS[scenario])
    assert await matrix.committed() == expected_rows


# ---------------------------------------------------------------------------------------------------------
# Isolation and read-only
# ---------------------------------------------------------------------------------------------------------


async def test_isolation_is_applied_to_the_connection(matrix: Matrix) -> None:
    dialect = matrix.backend.dialect
    assert await matrix.outer.serializable_level(dialect) == "SERIALIZABLE"
    if dialect == "postgresql":
        with pytest.raises(IllegalTransactionStateError, match="READ UNCOMMITTED"):
            await matrix.outer.read_uncommitted_level(dialect)
    else:
        assert await matrix.outer.read_uncommitted_level(dialect) == "READ UNCOMMITTED"
    # The level does not leak into the next unit on the same pooled connection.
    await matrix.outer.place("after-isolation")
    assert await matrix.committed() == ["after-isolation"]


async def test_read_only_refuses_writes(matrix: Matrix) -> None:
    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        await matrix.outer.orm_write_in_read_only()
    if matrix.backend.is_embedded:
        # SQLite has no read-only transaction; the ORM guard is the enforcement there.
        await matrix.outer.raw_write_in_read_only()
        assert await matrix.committed() == ["raw"]
        return
    with pytest.raises(DBAPIError, match="(?i)read.only"):
        await matrix.outer.raw_write_in_read_only()
    assert await matrix.committed() == []
    await matrix.outer.place("writable-again")  # the hint does not leak into the next unit
    assert await matrix.committed() == ["writable-again"]


async def test_a_core_write_in_a_read_only_unit_is_refused_before_it_runs(matrix: Matrix) -> None:
    """Refused by the unit's session on every backend, before the statement is sent: on PostgreSQL a read
    auto unit runs on AUTOCOMMIT, where the write would otherwise commit at once."""
    await matrix.outer.place("original")
    with pytest.raises(IllegalTransactionStateError, match="read-only"):
        await matrix.outer.core_write_in_read_only()
    with pytest.raises(IllegalTransactionStateError, match="read-only auto unit"):
        await matrix.outer.items.find_and_rename("renamed")
    assert await matrix.committed() == ["original"]


# ---------------------------------------------------------------------------------------------------------
# Read auto units on PostgreSQL: AUTOCOMMIT, unless a customizer needs a transaction
# ---------------------------------------------------------------------------------------------------------


def _log_wire_queries(engine: AsyncEngine, sink: list[str]) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.driver_connection.add_query_logger(lambda record: sink.append(record.query))


@pytest.mark.backends(PG)
async def test_a_read_auto_unit_sends_no_begin_on_postgresql(relational_backend: RelationalBackend) -> None:
    started = await _matrix(relational_backend)
    try:
        wire: list[str] = []
        _log_wire_queries(started.engine, wire)
        items = started.ctx.get_bean(MxItemRepository)
        await items.save(MxItem(name="w"))
        writes = [q for q in wire if q.upper().startswith("BEGIN")]
        assert len(writes) == 1  # the write auto unit is a transaction
        wire.clear()
        assert await items.count() == 1
        assert await items.find_by_id(1) is not None
        assert not [q for q in wire if q.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]
        wire.clear()
        await started.outer.place("x")  # the connection went back to the pool without AUTOCOMMIT
        assert [q for q in wire if q.upper().startswith("BEGIN")]
        await started.assert_clean()
    finally:
        await started.ctx.stop()


@pytest.mark.backends(PG)
async def test_a_customizer_gets_a_transactional_read_unit(relational_backend: RelationalBackend) -> None:
    started = await _matrix(relational_backend, TenantGuc)
    try:
        wire: list[str] = []
        _log_wire_queries(started.engine, wire)
        items = started.ctx.get_bean(MxItemRepository)
        assert await items.find_setting("app.tenant") == "acme"
        assert [q for q in wire if q.upper().startswith("BEGIN")]
        await started.assert_clean()
    finally:
        await started.ctx.stop()


# ---------------------------------------------------------------------------------------------------------
# Retries around transactions
# ---------------------------------------------------------------------------------------------------------


async def test_retry_outside_the_unit_commits_only_the_successful_attempt(matrix: Matrix) -> None:
    await matrix.outer.flaky_retry_outside()
    assert await matrix.committed() == ["attempt-2"]


async def test_retry_written_inside_still_runs_outside(matrix: Matrix) -> None:
    await matrix.outer.flaky_retry_written_inside()
    assert await matrix.committed() == ["attempt-2"]


async def test_a_retried_db_error_surfaces_as_itself_and_the_next_attempt_commits(matrix: Matrix) -> None:
    await matrix.outer.place("taken")
    await matrix.outer.duplicate_then_fixed()
    assert matrix.outer.attempts == 2
    assert await matrix.committed() == ["taken", "free"]


# ---------------------------------------------------------------------------------------------------------
# PostgreSQL: cancellation, unknown commit outcome, statement timeout
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends(PG)
async def test_a_cancel_during_a_statement_leaves_no_broken_connection(matrix: Matrix) -> None:
    for _ in range(3):
        with anyio.move_on_after(0.3) as scope:
            await matrix.outer.sleep_in_database(2)
        assert scope.cancelled_caught
        assert matrix.checked_out() == 0
        await matrix.outer.place(f"after-{_}")  # pre-ping is off: a poisoned connection would fail here
    assert await matrix.committed() == ["after-0", "after-1", "after-2"]


@pytest.mark.backends(PG)
async def test_two_cancels_during_a_held_back_rollback(relational_backend: RelationalBackend) -> None:
    upstream = make_url(relational_backend.url)
    proxy = PartitionProxy(upstream.host or "127.0.0.1", upstream.port or 5432)
    port = await proxy.start()
    proxied = RelationalBackend(PG, upstream.set(host="127.0.0.1", port=port).render_as_string(hide_password=False))
    await relational_backend.create_tables(MxItem)
    ctx = ApplicationContext(proxied.config())
    for bean in (RelationalAutoConfiguration, MxItemRepository, Inner, Outer):
        ctx.register_bean(bean)
    await ctx.start()
    started = Matrix(relational_backend, ctx)
    try:
        gate = Gate()
        task = asyncio.create_task(started.outer.fail_while_partitioned(proxy, gate))
        await gate.reached.wait()
        await asyncio.sleep(0.2)  # the ROLLBACK is on the wire, held back by the partition
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.2)
        assert not task.done()  # the shielded rollback is still waiting for the server
        proxy.heal()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert started.checked_out() == 0
        await started.outer.place("after-the-partition")
        assert await started.committed() == ["after-the-partition"]
        await started.assert_clean()
    finally:
        proxy.heal()
        await ctx.stop()
        await proxy.close()


@pytest.mark.backends(PG)
async def test_a_commit_that_loses_its_backend_has_an_unknown_outcome(matrix: Matrix, pg_server_url: str) -> None:
    admin_url = make_url(pg_server_url).set(database=make_url(matrix.backend.url).database)
    with pytest.raises(CommitOutcomeUnknownError):
        await matrix.outer.commit_loses_its_backend(admin_url.render_as_string(hide_password=False))
    assert matrix.outer.attempts == 1
    assert matrix.checked_out() == 0
    await matrix.outer.place("next")  # the pool dropped the dead connection
    assert await matrix.committed() == ["next"]


@pytest.mark.backends(PG)
async def test_a_timeout_also_bounds_the_statement_on_the_server(matrix: Matrix) -> None:
    assert await matrix.outer.statement_timeout() == "5s"
    with pytest.raises((TransactionTimedOutError, DBAPIError)):
        await matrix.outer.runaway()
    engine = create_async_engine(matrix.backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            running = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND state = 'active' AND query LIKE 'SELECT pg_sleep(5)%'"
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert running == 0


# ---------------------------------------------------------------------------------------------------------
# A Server-Sent Events client that disconnects mid-transaction (C062)
# ---------------------------------------------------------------------------------------------------------


@pytest.mark.backends(SQLITE_FILE, PG)
async def test_an_sse_client_disconnect_mid_transaction_leaves_the_pool_healthy(matrix: Matrix) -> None:
    import httpx
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route

    from pyfly.web.sse.adapters.starlette import make_sse_response

    outer = matrix.outer

    async def events(stream_number: str) -> AsyncIterator[dict[str, int]]:
        number = 0
        while True:
            await outer.save_event(f"event-{stream_number}-{number}")
            yield {"number": number}
            number += 1

    async def stream(request: Request) -> Response:
        return make_sse_response(events(request.query_params["n"]))

    async def plain(request: Request) -> Response:
        await outer.place(f"plain-{request.query_params['n']}")
        return Response("ok")

    app = Starlette(routes=[Route("/events", stream), Route("/plain", plain)])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for attempt in range(3):
                async with client.stream("GET", "/events", params={"n": attempt}) as response:
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            break  # one event read; leaving the block disconnects mid-transaction
                for _ in range(100):  # the server notices the disconnect and cancels the stream
                    if matrix.checked_out() == 0:
                        break
                    await asyncio.sleep(0.05)
                assert matrix.checked_out() == 0
                assert (await client.get("/plain", params={"n": attempt})).text == "ok"
    finally:
        server.should_exit = True
        await serving
    committed = sorted(await matrix.committed())
    # Each stream committed its first event; the unit open at the disconnect rolled back.
    assert committed == ["event-0-0", "event-1-0", "event-2-0", "plain-0", "plain-1", "plain-2"]


# ---------------------------------------------------------------------------------------------------------
# Holder shapes, fan-out, single-statement infrastructure units
# ---------------------------------------------------------------------------------------------------------


async def test_every_holder_shape_writes_inside_the_unit(relational_backend: RelationalBackend) -> None:
    started = await _matrix(relational_backend, Holders)
    try:
        holders = started.ctx.get_bean(Holders)
        with pytest.raises(ValueError):
            await holders.write_everywhere(fail=True)
        assert await started.committed() == []
        await holders.write_everywhere(fail=False)
        assert await started.committed() == ["deep", "dict", "provider", "session"]
        await started.assert_clean()
    finally:
        await started.ctx.stop()


async def test_gather_inside_a_unit_is_serialized_and_leaves_the_pool_healthy(
    relational_backend: RelationalBackend,
) -> None:
    started = await _matrix(relational_backend, Holders)
    try:
        await started.ctx.get_bean(Holders).fan_out()
        assert sorted(await started.committed()) == [f"child-{i}" for i in range(8)]
        for i in range(3):  # the next, unrelated units run on the same pooled connection
            await started.outer.place(f"after-{i}")
        assert len(await started.committed()) == 11
        await started.assert_clean()
    finally:
        await started.ctx.stop()


@pytest.mark.backends(PG)
async def test_a_single_statement_infrastructure_unit_runs_on_autocommit(
    relational_backend: RelationalBackend,
) -> None:
    started = await _matrix(relational_backend)
    try:
        wire: list[str] = []
        _log_wire_queries(started.engine, wire)
        async with infrastructure_unit("primary", single_statement=True) as session:
            await session.execute(text("INSERT INTO mx_item (name) VALUES ('outbox-row')"))
        assert not [q for q in wire if q.upper().startswith(("BEGIN", "COMMIT"))]
        assert await started.committed() == ["outbox-row"]
        await started.assert_clean()
    finally:
        await started.ctx.stop()
