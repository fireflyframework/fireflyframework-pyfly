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
"""Data-layer benchmark scenarios, measured through PyFly's public data API.

Every scenario boots a real ``ApplicationContext`` with ``RelationalAutoConfiguration``, a
``@repository`` on ``Repository[AuditItem, int]`` and a ``@service`` using ``@transactional``, on a
database of its own. The harness reaches the framework only through public entry points: the
context's beans (the engine and the ``async_sessionmaker``), repository methods and ``@transactional``.
The statements it writes by hand run on sessions it opens itself from that session factory, never on
a repository's internals. So the same scenarios measure the code before and after the unit-of-work
redesign (``tests/testing/test_data_benchmark_harness.py`` checks it). ``AuditItem`` is the model of
the audit proofs (``proofs.py``), which keeps ``p6`` and ``p7`` comparable with FINDINGS.md.

Scenarios:

``p6``          FINDINGS p6: a pooled unit of work on the session factory, the first one (which opens
                the connection) and 200 more; distinct server connections used; a fresh connection.
``p7``          FINDINGS p7: statements sent by ``save_all(100)`` and ``save(1)`` in ``@transactional``.
``tx``          The framework's unit of work: a ``@transactional`` method doing one ``count()``.
``read``        A repository read outside any transaction (``count()`` from a plain method).
``save``        Latency of ``save(1)`` and ``save_all(100)`` in ``@transactional``.
``exists``      ``exists_by_id`` and a derived ``exists_by_name`` over 5,000 matching rows: SQL shape
                and latency.
``derived``     CPU of a derived ``find_by_name`` against the same statement built by hand on each call
                and built once, both on a session the harness opens (interleaved; this thread's CPU
                time, so the database wait is out).
``stream``      ``stream_all()`` against ``find_all()`` over 5,000 rows.
``in_padding``  ``find_all_by_id`` for 1..64 ids: distinct SQL texts (asyncpg prepares one statement
                per text) and latency.
"""

from __future__ import annotations

import gc
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import String, bindparam, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.container.stereotypes import repository, service
from pyfly.context.application_context import ApplicationContext
from pyfly.data.relational.auto_configuration import RelationalAutoConfiguration
from pyfly.data.relational.sqlalchemy.entity import Base
from pyfly.data.relational.sqlalchemy.repository import Repository
from pyfly.data.transactional import transactional
from pyfly.testing import StatementCounter, pyfly_config


class AuditItem(Base):
    __tablename__ = "audit_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))


@repository
class AuditItemRepository(Repository[AuditItem, int]):
    async def find_by_name(self, name: str) -> list[AuditItem]: ...

    async def exists_by_name(self, name: str) -> bool: ...


_PREBUILT_FIND_BY_NAME = select(AuditItem).where(AuditItem.name == bindparam("name"))


async def hand_written_find(session: AsyncSession, name: str) -> list[AuditItem]:
    """The statement ``find_by_name`` derives, built by hand on every call."""
    result = await session.execute(select(AuditItem).where(AuditItem.name == name))
    return list(result.scalars().all())


async def prebuilt_find(session: AsyncSession, name: str) -> list[AuditItem]:
    """The same statement, built once: the floor a derived query can get down to."""
    result = await session.execute(_PREBUILT_FIND_BY_NAME, {"name": name})
    return list(result.scalars().all())


@service
class ItemService:
    def __init__(self, repo: AuditItemRepository, factory: async_sessionmaker[AsyncSession]) -> None:
        self.repo = repo
        # The service's own attribute. Before the redesign, @transactional dispatches on it; after, it
        # stays a legacy dispatch key, and the harness opens its own sessions from it (time_finders).
        self._session_factory = factory

    @transactional
    async def count(self) -> int:
        return await self.repo.count()

    async def count_outside_transaction(self) -> int:
        return await self.repo.count()

    @transactional
    async def save_one(self, name: str) -> None:
        await self.repo.save(AuditItem(name=name))

    @transactional
    async def save_many(self, n: int) -> None:
        await self.repo.save_all([AuditItem(name=f"bulk-{i}") for i in range(n)])

    @transactional
    async def exists_by_id(self, id_: int) -> bool:
        return await self.repo.exists_by_id(id_)

    @transactional
    async def exists_by_name(self, name: str) -> bool:
        return await self.repo.exists_by_name(name)

    @transactional
    async def time_finders(self, name: str, iterations: int) -> dict[str, dict[str, list[float]]]:
        """Wall and CPU seconds per call of the derived finder and of its two hand-written twins.

        The derived finder runs through the repository in this ``@transactional`` unit of work. The
        twins run on a session the harness opens itself from the injected ``async_sessionmaker``, once,
        before any call, and keeps in a transaction of its own; they touch no repository internals, so
        the scenario means the same before and after the redesign. The three run interleaved, so drift
        on the machine or the server affects them alike. CPU is this thread's time: the Python work of
        building, compiling and executing the statement, without the wait for the database (and
        without aiosqlite's worker thread).
        """
        async with self._session_factory() as session, session.begin():
            finders: dict[str, Callable[[], Awaitable[Any]]] = {
                "derived": lambda: self.repo.find_by_name(name),
                "hand_written": lambda: hand_written_find(session, name),
                "prebuilt": lambda: prebuilt_find(session, name),
            }
            for _ in range(max(1, iterations // 10)):  # warm-up: caches, prepared statements
                for call in finders.values():
                    await call()
            samples: dict[str, dict[str, list[float]]] = {label: {"wall": [], "cpu": []} for label in finders}
            gc.collect()
            gc.disable()
            try:
                for _ in range(iterations):
                    for label, call in finders.items():
                        wall, cpu = time.perf_counter(), time.thread_time()
                        await call()
                        samples[label]["cpu"].append(time.thread_time() - cpu)
                        samples[label]["wall"].append(time.perf_counter() - wall)
            finally:
                gc.enable()
        return samples

    @transactional
    async def stream_all(self) -> int:
        return sum([1 async for _ in self.repo.stream_all()])

    @transactional
    async def find_all(self) -> int:
        return len(await self.repo.find_all())

    @transactional
    async def find_all_by_id(self, ids: list[int]) -> int:
        return len(await self.repo.find_all_by_id(ids))


async def _time_calls(call: Callable[[], Awaitable[Any]], iterations: int) -> list[float]:
    for _ in range(max(1, iterations // 10)):  # warm-up: caches, prepared statements
        await call()
    samples: list[float] = []
    gc.collect()
    gc.disable()
    try:
        for _ in range(iterations):
            start = time.perf_counter()
            await call()
            samples.append(time.perf_counter() - start)
    finally:
        gc.enable()
    return samples


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """A booted application on a database of its own."""

    lane: str
    url: str
    context: ApplicationContext
    items: ItemService
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    lines: list[str] = field(default_factory=list)

    @property
    def is_pg(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    @property
    def connection_id_sql(self) -> str:
        """SQL naming the server connection, where the backend has one (else a plain ``SELECT 1``)."""
        return {
            "postgresql": "SELECT pg_backend_pid()",
            "mysql": "SELECT CONNECTION_ID()",
            "mariadb": "SELECT CONNECTION_ID()",
        }.get(self.engine.dialect.name, "SELECT 1")

    def say(self, line: str) -> None:
        self.lines.append(line)
        print(f"    {line}", flush=True)

    async def seed(self, rows: list[dict[str, Any]]) -> None:
        async with self.engine.begin() as conn:
            for start in range(0, len(rows), 1_000):
                await conn.execute(insert(AuditItem), rows[start : start + 1_000])


async def boot(lane: str, url: str) -> Scenario:
    """Start an application on *url* with the benchmark beans and ``ddl-auto: create``."""
    flat: dict[str, Any] = {
        "pyfly.data.relational.enabled": "true",
        "pyfly.data.relational.url": url,
        "pyfly.data.relational.ddl-auto": "create",
    }
    context = ApplicationContext(pyfly_config(base=flat))
    for bean in (RelationalAutoConfiguration, AuditItemRepository, ItemService):
        context.register_bean(bean)
    await context.start()
    return Scenario(
        lane=lane,
        url=url,
        context=context,
        items=context.get_bean(ItemService),
        engine=context.get_bean(AsyncEngine),
        factory=context.get_bean(async_sessionmaker),
    )


def ms(seconds: float) -> float:
    return round(seconds * 1_000, 3)


def us(seconds: float) -> float:
    return round(seconds * 1_000_000, 1)


def median_p95(samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    return statistics.median(ordered), ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def p6(env: Scenario) -> dict[str, Any]:
    connections: set[Any] = set()
    sql = text(env.connection_id_sql)

    async def one_unit() -> None:
        async with env.factory() as session, session.begin():
            connections.add((await session.execute(sql)).scalar())

    start = time.perf_counter()
    await one_unit()
    first = time.perf_counter() - start
    samples = []
    for _ in range(200):
        start = time.perf_counter()
        await one_unit()
        samples.append(time.perf_counter() - start)
    samples.sort()

    fresh_engine = create_async_engine(env.url)
    fresh = []
    try:
        for _ in range(10):
            start = time.perf_counter()
            async with fresh_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            fresh.append(time.perf_counter() - start)
            await fresh_engine.dispose()
    finally:
        await fresh_engine.dispose()
    fresh.sort()

    result = {
        "first_unit_ms": ms(first),
        "pooled_median_ms": ms(samples[100]),
        "pooled_p95_ms": ms(samples[190]),
        "distinct_connections": len(connections) if env.connection_id_sql != "SELECT 1" else None,
        "fresh_connection_median_ms": ms(fresh[5]),
    }
    env.say(f"first unit of work (opens the connection): {result['first_unit_ms']:.2f} ms")
    env.say(f"next 200 (pooled): median {result['pooled_median_ms']:.3f} ms, p95 {result['pooled_p95_ms']:.3f} ms")
    if result["distinct_connections"] is not None:
        env.say(f"distinct server connections used by 201 units: {result['distinct_connections']}")
    env.say(f"a fresh connection every time: median {result['fresh_connection_median_ms']:.2f} ms")
    return result


async def p7(env: Scenario) -> dict[str, Any]:
    with StatementCounter(env.engine) as counter:
        await env.items.save_many(100)
    save_all = counter.counts()
    save_all_commits = counter.commits
    with StatementCounter(env.engine) as counter:
        await env.items.save_one("single")
    save_one = counter.verbs()
    env.say(f"save_all(100 entities) -> statements sent: {save_all}")
    env.say(f"save(1 entity) -> statements sent: {save_one}")
    return {"save_all_100": save_all, "save_all_100_commits": save_all_commits, "save_1": save_one}


async def tx(env: Scenario) -> dict[str, Any]:
    start = time.perf_counter()
    await env.items.count()
    first = time.perf_counter() - start
    samples = await _time_calls(env.items.count, 200)
    with StatementCounter(env.engine) as counter:
        await env.items.count()
    median, p95 = median_p95(samples)
    result = {
        "first_ms": ms(first),
        "median_ms": ms(median),
        "p95_ms": ms(p95),
        "statements": counter.counts(),
        "commits": counter.commits,
        "rollbacks": counter.rollbacks,
    }
    env.say(f"@transactional count(): first {result['first_ms']:.2f} ms, median {result['median_ms']:.3f} ms")
    env.say(f"  p95 {result['p95_ms']:.3f} ms; one call sends {result['statements']}")
    env.say(f"  commits {result['commits']}, rollbacks {result['rollbacks']}")
    return result


async def read(env: Scenario) -> dict[str, Any]:
    samples = await _time_calls(env.items.count_outside_transaction, 200)
    with StatementCounter(env.engine) as counter:
        await env.items.count_outside_transaction()
    median, p95 = median_p95(samples)
    result = {
        "median_ms": ms(median),
        "p95_ms": ms(p95),
        "statements": counter.counts(),
        "commits": counter.commits,
        "rollbacks": counter.rollbacks,
    }
    env.say(f"count() outside a transaction: median {result['median_ms']:.3f} ms, p95 {result['p95_ms']:.3f} ms")
    env.say(f"  one call sends {result['statements']}, commits {result['commits']}, rollbacks {result['rollbacks']}")
    return result


async def save(env: Scenario) -> dict[str, Any]:
    one = await _time_calls(lambda: env.items.save_one("one"), 50)
    many = await _time_calls(lambda: env.items.save_many(100), 10)
    result = {"save_1_median_ms": ms(statistics.median(one)), "save_all_100_median_ms": ms(statistics.median(many))}
    env.say(f"@transactional save(1): median {result['save_1_median_ms']:.3f} ms")
    env.say(f"@transactional save_all(100): median {result['save_all_100_median_ms']:.2f} ms")
    return result


async def exists(env: Scenario) -> dict[str, Any]:
    await env.seed([{"name": "same"} for _ in range(5_000)])
    async with env.engine.connect() as conn:
        existing_id = (await conn.execute(select(AuditItem.id).limit(1))).scalar_one()
    result: dict[str, Any] = {}
    for label, call in (
        ("exists_by_id", lambda: env.items.exists_by_id(existing_id)),
        ("exists_by_name", lambda: env.items.exists_by_name("same")),
    ):
        samples = await _time_calls(call, 200)
        with StatementCounter(env.engine) as counter:
            assert await call()
        query = next(s.sql for s in counter.statements if s.verb == "SELECT")
        shape = " ".join(query.split())
        result[label] = {
            "median_ms": ms(statistics.median(samples)),
            "statements": counter.counts(),
            "sql": shape,
            "limit_1": "LIMIT" in shape.upper() or "EXISTS" in shape.upper(),
        }
        env.say(f"{label}: median {result[label]['median_ms']:.3f} ms, {result[label]['statements']}")
        env.say(f"  SQL: {shape[:110]}")
    return result


async def derived(env: Scenario) -> dict[str, Any]:
    await env.seed([{"name": f"item-{i}"} for i in range(1_000)])
    samples = await env.items.time_finders("item-7", 2_000)
    result: dict[str, Any] = {
        label: {clock: us(statistics.median(values)) for clock, values in clocks.items()}
        for label, clocks in samples.items()
    }
    result["cpu_overhead_vs_prebuilt_us"] = round(result["derived"]["cpu"] - result["prebuilt"]["cpu"], 1)
    for label, title in (
        ("derived", "find_by_name (derived)"),
        ("hand_written", "same statement built by hand per call"),
        ("prebuilt", "same statement built once"),
    ):
        env.say(f"{title}: median {result[label]['cpu']:.1f} us CPU, {result[label]['wall']:.1f} us wall per call")
    env.say(f"derived-query CPU over the prebuilt statement: {result['cpu_overhead_vs_prebuilt_us']:+.1f} us/call")
    return result


async def stream(env: Scenario) -> dict[str, Any]:
    rows = 5_000
    await env.seed([{"name": f"row-{i}"} for i in range(rows)])
    result: dict[str, Any] = {}
    for label, call in (("stream_all", env.items.stream_all), ("find_all", env.items.find_all)):
        samples = await _time_calls(call, 5)
        with StatementCounter(env.engine) as counter:
            assert await call() == rows
        median = statistics.median(samples)
        result[label] = {"median_ms": ms(median), "rows_per_s": round(rows / median), "statements": counter.counts()}
        env.say(
            f"{label}({rows} rows): median {result[label]['median_ms']:.1f} ms ({result[label]['rows_per_s']:,} rows/s)"
        )
    return result


async def in_padding(env: Scenario) -> dict[str, Any]:
    await env.seed([{"name": f"id-{i}"} for i in range(64)])
    async with env.engine.connect() as conn:
        ids = list((await conn.execute(select(AuditItem.id).order_by(AuditItem.id))).scalars())
    samples: list[float] = []
    with StatementCounter(env.engine) as counter:
        for n in range(1, 65):
            start = time.perf_counter()
            assert await env.items.find_all_by_id(ids[:n]) == n
            samples.append(time.perf_counter() - start)
    texts = {s.sql for s in counter.statements if s.verb == "SELECT"}
    result = {"calls": 64, "distinct_sql_texts": len(texts), "median_ms": ms(statistics.median(samples))}
    env.say(f"find_all_by_id(1..64 ids): {result['distinct_sql_texts']} distinct SQL texts for 64 calls")
    env.say(f"  median {result['median_ms']:.3f} ms per call")
    return result


SCENARIOS: dict[str, Callable[[Scenario], Awaitable[dict[str, Any]]]] = {
    "p6": p6,
    "p7": p7,
    "tx": tx,
    "read": read,
    "save": save,
    "exists": exists,
    "derived": derived,
    "stream": stream,
    "in_padding": in_padding,
}
