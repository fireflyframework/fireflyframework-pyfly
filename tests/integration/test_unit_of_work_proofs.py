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
"""The data-layer audit proofs, kept as regression tests of the unit of work (WP01).

``proofs.py`` p1-p8 and ``proofs2.py`` p8 and p12 booted a real ``ApplicationContext`` with the
relational auto-configuration, ``@repository`` beans and ``@transactional`` services, and printed what
the framework really did. Each test below runs the same scenario and asserts the line the audit marked
"expected", on a SQLite file database (foreign keys on) and on PostgreSQL. After every scenario no
pooled connection is checked out and PostgreSQL shows no backend ``idle in transaction``.

- p1 (F1): two concurrent ``@transactional`` calls through one singleton service keep their own units.
- p2 (F3): a repository write outside ``@transactional`` commits; on SQLite no lock is left behind.
- p2b and the WP02 gate: a read outside a transaction sees what other connections committed since, and
  on a SQLite file no read snapshot outlives the call (the WAL can checkpoint).
- p3 (F6): ``isolation=`` runs.
- p4 (F2): ``REQUIRES_NEW`` inside ``REQUIRED`` resumes the outer unit. SQLite has one writer, so
  there the inner unit fails fast instead of waiting for the lock its own suspended caller holds.
- p5 (F7): a write inside ``read_only=True`` is refused and nothing commits.
- p6: pooled units of work reuse one server connection.
- p7 (F11, measured): ``save_all(100)`` inside one unit commits once. The per-entity ``SELECT`` that
  ``refresh`` sends belongs to WP03, which owns the repository's statement cost.
- p8 (F4): repositories used outside a transaction pin no connection, so a pool of two still serves the
  next ``@transactional`` call.
- p12 (F5): after the server drops every connection, the next plain read succeeds (a read auto unit
  retries once on a connection-invalidated error) with pool pre-ping off.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from sqlalchemy import Identity, Integer, String, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.relational.sqlalchemy.transactional import _active_session_var
from pyfly.data.transactional import Isolation, Propagation, transactional
from pyfly.testing.statement_counter import StatementCounter
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)


class ProofItem(Base):
    __tablename__ = "uow_proof_item"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


class ProofCustomer(Base):
    __tablename__ = "uow_proof_customer"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class ProofItemRepository(Repository[ProofItem, int]):
    pass


@repository
class ProofCustomerRepository(Repository[ProofCustomer, int]):
    pass


# Repository calls that ran on a session other than the unit their caller bound (p1).
MISMATCHES: list[str] = []


def _check(tag: str, repo: ProofItemRepository) -> None:
    active = _active_session_var.get()
    if active is not None and repo._session is not active:
        MISMATCHES.append(tag)


@service
class ProofAuditService:
    def __init__(self, repo: ProofItemRepository, factory: async_sessionmaker[AsyncSession]) -> None:
        self.repo = repo
        self._session_factory = factory

    @transactional(propagation=Propagation.REQUIRES_NEW)
    async def log(self, name: str) -> None:
        await self.repo.save(ProofItem(name=name))


@service
class ProofItemService:
    def __init__(
        self,
        repo: ProofItemRepository,
        customers: ProofCustomerRepository,
        factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.repo = repo
        self.customers = customers
        self._session_factory = factory

    @transactional
    async def create_pair(self, name: str, *, fail: bool, pause: float) -> None:
        _check(f"{name}-a", self.repo)
        await self.repo.save(ProofItem(name=f"{name}-a"))
        await asyncio.sleep(pause)  # any await: an HTTP call, another query, a cache read...
        _check(f"{name}-b", self.repo)
        await self.repo.save(ProofItem(name=f"{name}-b"))
        await asyncio.sleep(pause)
        if fail:
            raise ValueError(f"{name} fails and must leave nothing behind")

    @transactional
    async def outer_with_audit(self, audit: ProofAuditService) -> None:
        await self.repo.save(ProofItem(name="outer-before"))
        await audit.log("audit-row")
        await self.repo.save(ProofItem(name="outer-after"))

    @transactional(isolation=Isolation.SERIALIZABLE)
    async def serializable(self) -> int:
        return await self.repo.count()

    @transactional(read_only=True)
    async def read_only_that_writes(self) -> None:
        await self.repo.save(ProofItem(name="written-inside-read-only"))

    @transactional
    async def place(self, name: str) -> None:
        await self.repo.save(ProofItem(name=name))

    @transactional
    async def save_many(self, n: int) -> None:
        await self.repo.save_all([ProofItem(name=f"bulk-{i}") for i in range(n)])

    @transactional
    async def backend_pid(self) -> int:
        return int((await self.repo._session.execute(text("SELECT pg_backend_pid()"))).scalar_one())

    async def not_transactional_save(self, name: str) -> None:
        await self.repo.save(ProofItem(name=name))

    async def not_transactional_find(self, id_: int) -> str | None:
        item = await self.repo.find_by_id(id_)
        return item.name if item else None

    async def not_transactional_count(self) -> int:
        return await self.repo.count()


_BEANS = (
    RelationalAutoConfiguration,
    ProofItemRepository,
    ProofCustomerRepository,
    ProofItemService,
    ProofAuditService,
)


async def _boot(backend: RelationalBackend, overrides: dict[str, Any] | None = None) -> ApplicationContext:
    await backend.create_tables(ProofItem, ProofCustomer)
    ctx = ApplicationContext(backend.config(overrides))
    for bean in _BEANS:
        ctx.register_bean(bean)
    await ctx.start()
    return ctx


async def _committed(backend: RelationalBackend) -> list[str]:
    """What another process sees: an engine of its own, not the application's pool."""
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT name FROM uow_proof_item ORDER BY id"))).all()
            return [row[0] for row in rows]
    finally:
        await engine.dispose()


async def _idle_in_transaction(backend: RelationalBackend) -> list[str]:
    """PostgreSQL backends of this database left ``idle in transaction`` (always ``[]`` on SQLite)."""
    if backend.dialect != "postgresql":
        return []
    engine = create_async_engine(backend.url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT left(query, 60) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
                )
            )
            return [row[0] for row in rows.all()]
    finally:
        await engine.dispose()


def _checked_out(ctx: ApplicationContext) -> int:
    registry = ctx.get_bean(DataSourceRegistry)
    return sum(ds.engine.sync_engine.pool.checkedout() for ds in registry.all_datasources())


async def _assert_clean(backend: RelationalBackend, ctx: ApplicationContext) -> None:
    assert _checked_out(ctx) == 0
    assert await _idle_in_transaction(backend) == []


async def test_p1_concurrent_transactions_keep_their_own_units(relational_backend: RelationalBackend) -> None:
    MISMATCHES.clear()
    ctx = await _boot(relational_backend)
    try:
        svc = ctx.get_bean(ProofItemService)
        results = await asyncio.gather(
            svc.create_pair("A", fail=False, pause=0.05),
            svc.create_pair("B", fail=True, pause=0.02),
            return_exceptions=True,
        )
        assert results[0] is None
        assert isinstance(results[1], ValueError)
        assert await _committed(relational_backend) == ["A-a", "A-b"]
        assert MISMATCHES == []
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p2_repository_write_outside_a_transaction_commits(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        await ctx.get_bean(ProofItemService).not_transactional_save("saved-outside-tx")
        assert await _committed(relational_backend) == ["saved-outside-tx"]
        await _assert_clean(relational_backend, ctx)
        if relational_backend.is_embedded:
            # No write transaction is left open, so another writer gets the lock at once.
            other = create_async_engine(relational_backend.url, poolclass=NullPool, connect_args={"timeout": 1})
            try:
                async with other.begin() as conn:
                    await conn.execute(text("INSERT INTO uow_proof_item (name) VALUES ('other-writer')"))
            finally:
                await other.dispose()
    finally:
        await ctx.stop()
    assert "saved-outside-tx" in await _committed(relational_backend)


async def test_p2b_reads_outside_a_transaction_see_later_commits(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        svc = ctx.get_bean(ProofItemService)
        other = create_async_engine(relational_backend.url, poolclass=NullPool)
        try:
            assert await svc.not_transactional_count() == 0
            async with other.begin() as conn:
                await conn.execute(text("INSERT INTO uow_proof_item (name) VALUES ('v1')"))
            # WP02's gate: a long-lived repository session held the first read's snapshot and read 0.
            assert await svc.not_transactional_count() == 1
            async with other.connect() as conn:
                first_id = int((await conn.execute(text("SELECT min(id) FROM uow_proof_item"))).scalar_one())
            assert await svc.not_transactional_find(first_id) == "v1"
            async with other.begin() as conn:
                await conn.execute(text("UPDATE uow_proof_item SET name = 'v2' WHERE id = :id"), {"id": first_id})
            assert await svc.not_transactional_find(first_id) == "v2"
            await svc.place("added-in-a-transaction")
            assert await svc.not_transactional_count() == 2
            if relational_backend.is_embedded:
                # No reader holds a snapshot, so a full checkpoint is not blocked (busy == 0).
                async with other.connect() as conn:
                    busy, _log, _checkpointed = (await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))).one()
                assert busy == 0
        finally:
            await other.dispose()
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p3_isolation_runs(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        assert await ctx.get_bean(ProofItemService).serializable() == 0
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p4_requires_new_resumes_the_outer_unit(relational_backend: RelationalBackend) -> None:
    from pyfly.data.transaction import IllegalTransactionStateError

    ctx = await _boot(relational_backend)
    try:
        svc = ctx.get_bean(ProofItemService)
        audit = ctx.get_bean(ProofAuditService)
        if relational_backend.is_embedded:
            started = time.perf_counter()
            with pytest.raises(IllegalTransactionStateError, match="REQUIRES_NEW"):
                await svc.outer_with_audit(audit)
            assert time.perf_counter() - started < 2.0  # not a busy_timeout wait on its own lock
            assert await _committed(relational_backend) == []
        else:
            await svc.outer_with_audit(audit)
            assert sorted(await _committed(relational_backend)) == ["audit-row", "outer-after", "outer-before"]
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p5_a_write_inside_read_only_is_refused(relational_backend: RelationalBackend) -> None:
    from pyfly.data.transaction import IllegalTransactionStateError

    ctx = await _boot(relational_backend)
    try:
        with pytest.raises(IllegalTransactionStateError, match="read-only"):
            await ctx.get_bean(ProofItemService).read_only_that_writes()
        assert await _committed(relational_backend) == []
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p6_pooled_units_reuse_one_connection(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        svc = ctx.get_bean(ProofItemService)
        if relational_backend.dialect == "postgresql":
            pids = {await svc.backend_pid() for _ in range(50)}
            assert len(pids) == 1
        else:
            for i in range(50):
                await svc.place(f"u{i}")
            assert len(await _committed(relational_backend)) == 50
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p7_save_all_commits_once(relational_backend: RelationalBackend) -> None:
    ctx = await _boot(relational_backend)
    try:
        engine = ctx.get_bean(AsyncEngine)
        with StatementCounter(engine) as counter:
            await ctx.get_bean(ProofItemService).save_many(100)
        assert counter.commits == 1
        assert counter.rollbacks == 0
        assert len(await _committed(relational_backend)) == 100
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


async def test_p8_repositories_outside_a_transaction_pin_no_connection(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(
        relational_backend,
        {
            "pyfly.data.relational.pool.size": "2",
            "pyfly.data.relational.pool.max-overflow": "0",
            "pyfly.data.relational.pool.timeout": "3",
        },
    )
    try:
        svc = ctx.get_bean(ProofItemService)
        assert await svc.repo.find_by_id(1) is None  # a GET /items/1 controller calling the repository
        assert await svc.customers.find_by_id(1) is None  # a GET /customers/1
        await _assert_clean(relational_backend, ctx)
        await svc.place("after-two-reads")
        assert await _committed(relational_backend) == ["after-two-reads"]
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()


@pytest.mark.backends(PG)
async def test_p12_a_plain_read_recovers_after_the_server_drops_its_connections(
    relational_backend: RelationalBackend,
) -> None:
    ctx = await _boot(relational_backend)
    try:
        assert ctx.get_bean(AsyncEngine).sync_engine.pool._pre_ping is False
        svc = ctx.get_bean(ProofItemService)
        # Warm the pool with two connections, as a busy application would have.
        await asyncio.gather(svc.place("warm-1"), svc.place("warm-2"))
        admin = create_async_engine(relational_backend.url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                dropped = (
                    await conn.execute(
                        text(
                            "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                            "AND backend_type = 'client backend'"
                        )
                    )
                ).scalar_one()
        finally:
            await admin.dispose()
        assert dropped >= 1
        for _ in range(3):
            assert await svc.not_transactional_count() == 2
        await svc.place("inside-tx")
        assert sorted(await _committed(relational_backend)) == ["inside-tx", "warm-1", "warm-2"]
        await _assert_clean(relational_backend, ctx)
    finally:
        await ctx.stop()
