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
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.cache.adapters.postgres import PostgresCacheAdapter
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import FrameworkSchemaError, cache_entries
from pyfly.data.relational.sqlalchemy.transaction_manager import SqlAlchemyTransactionManager
from pyfly.data.transaction import TransactionTemplate
from tests.support.backend_matrix import PG, RelationalBackend


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
