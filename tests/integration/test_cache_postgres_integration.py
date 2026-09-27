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
"""The SQL cache adapter (``pyfly.cache.provider=postgres``) on every relational lane.

PostgreSQL is its main target; the SQLite file lane runs in the fast suite, and PostgreSQL, MySQL and
MariaDB with ``-m integration``. Covers the audit's cache defects: expiries bound as naive values into
``TIMESTAMPTZ`` and shifted by the process time zone (C091), expired keys that ``put_if_absent`` could never
take again and rows never purged (C092), and three round trips for every single statement (C093, C094).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.cache.adapters.postgres import MAX_KEY_LENGTH, PostgresCacheAdapter
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import FrameworkSchemaError, cache_entries
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from tests.support.backend_matrix import MARIADB, MYSQL, PG, RelationalBackend


@dataclass(frozen=True)
class _LongNamedQuery(Query[int]):
    sku: str = "a"


_LongNamedQuery.__name__ = _LongNamedQuery.__qualname__ = "LongNamed" * 11 + "Query"  # a 104-character class name


@query_handler(cacheable=True)
class _LongNamedHandler(QueryHandler[_LongNamedQuery, int]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: _LongNamedQuery) -> int:
        self.calls += 1
        return 42


async def _cache(backend: RelationalBackend, **options: Any) -> PostgresCacheAdapter:
    cache = PostgresCacheAdapter(backend.create_engine(), **options)
    await cache.start()
    return cache


async def _rows(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(select(func.count()).select_from(cache_entries))).scalar_one())


async def test_the_cache_round_trips_on_every_backend(relational_backend: RelationalBackend) -> None:
    """The table was PostgreSQL DDL (``BYTEA``, a ``TEXT`` key) and the upserts ``ON CONFLICT``: the adapter
    now runs wherever SQLAlchemy does."""
    cache = await _cache(relational_backend)

    await cache.put("k", {"a": 1})
    await cache.put("k", {"a": 2})
    await cache.put("scalar", 42)
    assert await cache.get("k") == {"a": 2}
    assert await cache.get("scalar") == 42
    assert await cache.get("missing") is None
    assert await cache.exists("k") is True and await cache.exists("missing") is False

    await cache.put("p:1", 1)
    await cache.put("p:2", 2)
    await cache.put("p_3", 3)  # "_" is a LIKE wildcard: it must not match "p:"
    await cache.put("100%!", 4)
    assert sorted(await cache.get_keys("p:*")) == ["p:1", "p:2"]
    assert await cache.get_keys("100%!") == ["100%!"]
    assert await cache.evict_by_prefix("p:") == 2
    assert await cache.get("p_3") == 3
    assert await cache.evict("p_3") is True and await cache.evict("p_3") is False

    stats = await cache.get_stats()
    assert stats["type"] == "postgres" and stats["hits"] == 3 and stats["misses"] == 1 and stats["size"] == 3

    await cache.clear()
    assert await cache.get("k") is None and await _rows(cache.engine) == 0


async def test_keys_match_exactly_on_every_backend(relational_backend: RelationalBackend) -> None:
    """MySQL and MariaDB compared keys with the database's default collation, which ignores case and accents
    (and trailing spaces on MariaDB): ``User:1`` read the entry of ``user:1``, as PostgreSQL and SQLite never
    did."""
    cache = await _cache(relational_backend)
    keys = ["user:1", "User:1", "cafe", "café", "pad", "pad "]

    for value, key in enumerate(keys):
        await cache.put(key, value)

    assert [await cache.get(key) for key in keys] == list(range(len(keys)))
    assert sorted(await cache.get_keys("user:*")) == ["user:1"]
    assert await cache.evict_by_prefix("User") == 1
    assert await cache.get("user:1") == 0
    assert await cache.put_if_absent("CAFE", "new") is True


async def test_an_entry_expires_after_its_ttl(relational_backend: RelationalBackend) -> None:
    cache = await _cache(relational_backend)
    await cache.put("short", "alive", ttl=timedelta(milliseconds=300))
    await cache.put("long", "alive", ttl=timedelta(hours=1))

    assert await cache.get("short") == "alive"
    await asyncio.sleep(0.5)
    assert await cache.get("short") is None and await cache.exists("short") is False
    assert await cache.get("long") == "alive"
    assert await cache.get_keys("*") == ["long"]


async def test_put_if_absent_takes_a_key_whose_entry_expired(relational_backend: RelationalBackend) -> None:
    """C092: an expired row kept its key, so ``put_if_absent`` returned ``False`` forever and a lock or dedupe
    marker with a TTL never came free."""
    node_a, node_b = await _cache(relational_backend), await _cache(relational_backend)

    assert await node_a.put_if_absent("lock", "a", ttl=timedelta(milliseconds=300)) is True
    assert await node_b.put_if_absent("lock", "b", ttl=timedelta(milliseconds=300)) is False
    await asyncio.sleep(0.5)
    assert await node_b.put_if_absent("lock", "b", ttl=timedelta(seconds=30)) is True
    assert await node_a.get("lock") == "b"
    assert await node_a.put_if_absent("lock", "a") is False
    assert await node_a.put_if_absent("forever", "x") is True  # no TTL: never taken over
    assert await node_b.put_if_absent("forever", "y") is False


async def test_one_of_many_racing_callers_takes_a_free_or_expired_key(relational_backend: RelationalBackend) -> None:
    caches = [await _cache(relational_backend) for _ in range(6)]
    await caches[0].put("contended", "stale", ttl=timedelta(milliseconds=1))
    await asyncio.sleep(0.05)

    for key in ("contended", "fresh"):
        outcomes = await asyncio.gather(
            *(cache.put_if_absent(key, index, ttl=timedelta(minutes=1)) for index, cache in enumerate(caches))
        )
        assert outcomes.count(True) == 1
        assert await caches[0].get(key) == outcomes.index(True)


async def test_expired_rows_are_purged(relational_backend: RelationalBackend) -> None:
    """C092: nothing deleted expired rows; short-lived entries accumulated without bound."""
    cache = await _cache(relational_backend, purge_interval=None)
    cache.PURGE_BATCH = 7  # several batches
    for index in range(20):
        await cache.put(f"short-{index}", index, ttl=timedelta(milliseconds=1))
    await cache.put("live", "kept", ttl=timedelta(hours=1))
    await cache.put("forever", "kept")
    await asyncio.sleep(0.05)

    assert await _rows(cache.engine) == 22
    assert await cache.purge_expired() == 20
    assert await _rows(cache.engine) == 2
    assert await cache.get("live") == "kept" and await cache.get("forever") == "kept"


async def test_writes_purge_expired_rows_once_per_interval(relational_backend: RelationalBackend) -> None:
    cache = await _cache(relational_backend, purge_interval=timedelta(milliseconds=200))
    for index in range(5):
        await cache.put(f"short-{index}", index, ttl=timedelta(milliseconds=1))
    assert await _rows(cache.engine) == 5  # within the interval: no purge yet

    await asyncio.sleep(0.3)
    await cache.put("trigger", "x")
    assert await _rows(cache.engine) == 1


async def test_a_write_purges_one_batch_and_the_next_writes_the_rest(relational_backend: RelationalBackend) -> None:
    """The write that found a purge due deleted every expired row before returning: a backlog (the rows of the
    releases that never purged) landed on one request, which grew with it (745 ms for 300k rows). A write now
    purges one batch; while a full batch comes back, the next write purges the next one, and once the backlog
    is gone the interval applies again."""
    cache = await _cache(relational_backend, purge_interval=timedelta(seconds=1))
    cache.PURGE_BATCH = 4
    for index in range(10):
        await cache.put(f"backlog-{index}", index, ttl=timedelta(milliseconds=1))
    await asyncio.sleep(1.1)

    rows = []
    for key in ("a", "b", "c"):
        await cache.put(key, key)
        rows.append(await _rows(cache.engine))
    assert rows == [10 + 1 - 4, 7 + 1 - 4, 4 + 1 - 2]  # 4 expired rows, then 4, then the last 2

    for index in range(3):
        await cache.put(f"later-{index}", index, ttl=timedelta(milliseconds=1))
    await asyncio.sleep(0.05)
    await cache.put("d", "d")
    assert await _rows(cache.engine) == 3 + 3 + 1  # within the interval again: nothing purged
    assert await cache.purge_expired() == 3


async def test_a_write_in_a_transaction_that_rolls_back_is_rolled_back(relational_backend: RelationalBackend) -> None:
    """The adapter joins the unit of work on its datasource: an entry does not outlive the rollback of the
    transaction that wrote it."""
    engine = relational_backend.create_engine()
    cache = PostgresCacheAdapter(engine)
    await cache.start()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_engine(engine))

    with pytest.raises(RuntimeError):
        async with template.transaction():
            await cache.put("rolled-back", 1)
            raise RuntimeError("the business step failed")
    async with template.transaction():
        await cache.put("committed", 2)

    assert await cache.get("rolled-back") is None and await cache.get("committed") == 2


@pytest.fixture
async def registry(relational_backend: RelationalBackend) -> AsyncIterator[DataSourceRegistry]:
    """The registry's datasource: the production engine (WAL on SQLite, where a reader and a writer overlap)."""
    datasources = DataSourceRegistry(relational_backend.config())
    try:
        yield datasources
    finally:
        await datasources.close()


async def test_a_write_inside_a_read_only_transaction_gets_a_unit_of_its_own(registry: DataSourceRegistry) -> None:
    """A read-only unit refuses writes; a cache write made in one (a read that caches its result) commits in
    a unit of its own instead of failing the read."""
    datasource = registry.primary
    cache = PostgresCacheAdapter(datasource)
    await cache.start()
    template = TransactionTemplate(SqlAlchemyTransactionManager.for_datasource(datasource), read_only=True)

    async with template.transaction():
        assert await cache.get("report") is None
        await cache.put("report", {"total": 3})
        assert await cache.put_if_absent("report-lock", "me") is True

    assert await cache.get("report") == {"total": 3} and await cache.get("report-lock") == "me"


# ---------------------------------------------------------------------------------------------------------
# The CacheAdapter contract (WP14): an own namespace, named caches, keys that fit
# ---------------------------------------------------------------------------------------------------------


async def _stored_keys(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        rows = (await connection.execute(select(cache_entries.c.cache_key).order_by(cache_entries.c.cache_key))).all()
    return [str(key) for (key,) in rows]


async def test_clear_deletes_only_this_caches_namespace(relational_backend: RelationalBackend) -> None:
    """The table is shared: by the dedicated caches of ``with_namespace``, by caches with another namespace,
    by other applications. ``clear()`` used to empty it; it deletes this cache's entries only now."""
    cache = await _cache(relational_backend)
    idempotency = cache.with_namespace("idempotency")
    other = PostgresCacheAdapter(cache.engine, namespace="other-app")
    await cache.put("k", 1)
    await cache.put("p:1", 2)
    await idempotency.put("k", "record")
    await other.put("k", "theirs")
    async with cache.engine.begin() as connection:
        await connection.execute(cache_entries.insert().values(cache_key="foreign", value=b"1", expires_at=None))

    assert await _stored_keys(cache.engine) == [
        "foreign",
        "other-app:k",
        "pyfly:cache.idempotency:k",
        "pyfly:cache:k",
        "pyfly:cache:p:1",
    ]
    assert sorted(await cache.get_keys()) == ["k", "p:1"]
    assert (await cache.get_stats())["size"] == 2
    assert await idempotency.get("k") == "record" and await cache.get("k") == 1

    assert await cache.evict_by_prefix("") == 2  # every key of its own, and no one else's
    await cache.put("k", 1)
    await cache.clear()
    assert await _stored_keys(cache.engine) == ["foreign", "other-app:k", "pyfly:cache.idempotency:k"]
    assert await idempotency.get("k") == "record" and await other.get("k") == "theirs"

    await idempotency.clear()
    assert await _stored_keys(cache.engine) == ["foreign", "other-app:k"]


async def test_an_empty_namespace_owns_the_table_and_says_so_for_a_dedicated_cache(
    relational_backend: RelationalBackend, caplog: pytest.LogCaptureFixture
) -> None:
    cache = await _cache(relational_backend, namespace="")
    caplog.set_level(logging.WARNING, logger="pyfly.cache.adapters.postgres")
    records = cache.with_namespace("idempotency")
    cache.with_namespace("orchestration")
    assert records.namespace == "idempotency:"
    assert len([r for r in caplog.records if "cache_not_dedicated" in r.getMessage()]) == 1  # once per cache
    await cache.put("k", 1)
    await records.put("k", "record")
    assert sorted(await cache.get_keys()) == ["idempotency:k", "k"]
    await cache.clear()  # it owns the table: the dedicated cache goes too
    assert await _rows(cache.engine) == 0


async def test_a_glob_escape_matches_a_wildcard_literally(relational_backend: RelationalBackend) -> None:
    """A region (``PrefixedCache``) lists its keys with its prefix escaped as a Redis ``MATCH`` pattern
    escapes it: a backslash makes the next wildcard literal."""
    cache = await _cache(relational_backend)
    await cache.put("a*b:1", 1)
    await cache.put("aXb:2", 2)
    assert sorted(await cache.get_keys("a*b:*")) == ["a*b:1", "aXb:2"]
    assert await cache.get_keys("a\\*b:*") == ["a*b:1"]


@pytest.mark.backends(MYSQL, MARIADB)
async def test_a_key_longer_than_the_column_is_refused_before_any_write(relational_backend: RelationalBackend) -> None:
    """The key column is VARCHAR(512) on MySQL and MariaDB, the namespace included. A longer key is refused
    with ValueError before anything is written; the query cache logs that and does not cache."""
    from pyfly.cqrs.cache.adapter import QueryCacheAdapter

    cache = await _cache(relational_backend)
    fits = "k" * (MAX_KEY_LENGTH - len(cache.namespace))
    await cache.put(fits, 1)
    assert await cache.get(fits) == 1
    too_long = fits + "k"
    with pytest.raises(ValueError, match="at most 512 characters"):
        await cache.put(too_long, 1)
    with pytest.raises(ValueError, match="at most 512 characters"):
        await cache.put_if_absent(too_long, 1)

    queries = QueryCacheAdapter(cache)
    await queries.put(too_long, "answer")  # logged and skipped: a cache problem never fails the query
    assert await queries.entry_key(too_long, "0" * 64) is None  # no generation could start: uncached
    assert await _stored_keys(cache.engine) == [cache.namespace + fits]


@pytest.mark.backends(MYSQL, MARIADB)
async def test_the_query_cache_keys_of_a_long_handler_name_fit_the_key_column(
    relational_backend: RelationalBackend,
) -> None:
    """A scoped query-cache entry is ``:cqrs:<Class>:<64 hex>|<generation>|scope=<64 hex>`` in the cache's
    namespace: about 170 characters plus the class name, well within the 512 the key column takes."""
    from pyfly.cqrs.cache.adapter import QueryCacheAdapter
    from pyfly.cqrs.command.registry import HandlerRegistry
    from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
    from pyfly.cqrs.query.bus import DefaultQueryBus

    cache = await _cache(relational_backend)
    handler = _LongNamedHandler()
    registry = HandlerRegistry()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    alice = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()

    assert await bus.query_with_context(_LongNamedQuery(sku="a"), alice) == 42
    assert await bus.query_with_context(_LongNamedQuery(sku="a"), alice) == 42
    assert handler.calls == 1  # cached
    keys = await _stored_keys(cache.engine)
    assert len(keys) == 2 and all(len(key) < MAX_KEY_LENGTH for key in keys)  # its generation and its entry


async def test_without_ddl_a_missing_table_fails_fast_at_start(relational_backend: RelationalBackend) -> None:
    cache = PostgresCacheAdapter(relational_backend.create_engine(), create_table=False)
    with pytest.raises(FrameworkSchemaError, match="table pyfly_cache_entries does not exist"):
        await cache.start()


async def test_start_and_stop_are_idempotent(relational_backend: RelationalBackend) -> None:
    cache = await _cache(relational_backend)
    await cache.start()
    await cache.stop()
    await cache.stop()
    await cache.put("still", "works")  # the engine was never the adapter's to close
    assert await cache.get("still") == "works"


@pytest.fixture
def process_time_zone() -> Iterator[Any]:
    """Switch the process's local time zone (``TZ`` + ``time.tzset``), restored afterwards."""
    previous = os.environ.get("TZ")

    def switch(zone: str) -> None:
        os.environ["TZ"] = zone
        time.tzset()

    try:
        yield switch
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.backends(PG)
async def test_expiry_does_not_move_with_the_process_time_zone(
    relational_backend: RelationalBackend, process_time_zone: Any
) -> None:
    """C091: a naive "UTC" value bound into ``TIMESTAMPTZ`` is read by asyncpg as local time: at +02:00 a
    ten-minute entry was stored as expired two hours ago, and nodes in different zones disagreed."""
    engine = relational_backend.create_engine()
    madrid_node, utc_node = PostgresCacheAdapter(engine), PostgresCacheAdapter(engine)
    await madrid_node.start()

    process_time_zone("Europe/Madrid")
    await madrid_node.put("shared", "value", ttl=timedelta(minutes=10))
    process_time_zone("America/New_York")
    await madrid_node.put("other", "value", ttl=timedelta(minutes=10))
    process_time_zone("UTC")

    async with engine.connect() as connection:
        remaining = (
            await connection.execute(
                text("SELECT extract(epoch FROM expires_at - now()) FROM pyfly_cache_entries ORDER BY cache_key")
            )
        ).scalars()
        seconds = [float(value) for value in remaining]
    assert all(9 * 60 < value <= 10 * 60 for value in seconds), seconds
    assert await utc_node.get("shared") == "value" and await utc_node.exists("other") is True


def _log_wire_queries(engine: AsyncEngine, sink: list[str]) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _attach(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.driver_connection.add_query_logger(lambda record: sink.append(record.query))


@pytest.mark.backends(PG)
async def test_each_operation_is_one_round_trip_on_postgresql(relational_backend: RelationalBackend) -> None:
    """C093/C094: every cache call was ``BEGIN`` + statement + ``COMMIT``/``ROLLBACK``: a cache meant to
    save database time paid three round trips per hit."""
    engine = relational_backend.create_engine()
    wire: list[str] = []
    _log_wire_queries(engine, wire)
    cache = PostgresCacheAdapter(engine, purge_interval=None)
    await cache.start()
    wire.clear()

    await cache.put("k", 1, ttl=timedelta(minutes=1))
    assert await cache.get("k") == 1
    assert await cache.exists("k") is True
    assert await cache.put_if_absent("k2", 2, ttl=timedelta(minutes=1)) is True
    await cache.get_keys("k*")
    await cache.evict("k")
    await cache.purge_expired()

    assert not [q for q in wire if q.strip().upper().startswith(("BEGIN", "COMMIT", "ROLLBACK"))]


async def test_the_context_wires_the_sql_cache_and_creates_its_table(relational_backend: RelationalBackend) -> None:
    from pyfly.cache.ports.outbound import CacheAdapter
    from pyfly.context.application_context import ApplicationContext

    ctx = ApplicationContext(
        relational_backend.config(
            {
                "pyfly.data.relational.enabled": "false",  # ddl-auto=create would create every model of the run
                "pyfly.data.relational.ddl-auto": "create",
                "pyfly.cache.enabled": "true",
                "pyfly.cache.provider": "postgres",
            }
        )
    )
    await ctx.start()
    try:
        cache = ctx.get_bean(CacheAdapter)  # type: ignore[type-abstract]
        assert isinstance(cache, PostgresCacheAdapter)
        await cache.put("wired", {"ok": True}, ttl=timedelta(minutes=1))
        assert await cache.get("wired") == {"ok": True}
    finally:
        await ctx.stop()
