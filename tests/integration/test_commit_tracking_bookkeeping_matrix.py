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
"""The framework's bookkeeping writes are not a call's commits (``untracked()``), on sqlite-file and PostgreSQL.

``track_commits()`` tells an orchestration engine whether a step committed before it failed: a step that did
is compensated and never retried. Some writes the framework makes on a call's way are not the call's effects
but idempotent bookkeeping, which any later call would make too. They run in ``untracked()`` blocks, which no
tracker sees, so a step that only read is still retried when it fails:

- a read of the event store's global stream gives the committed events waiting for a position theirs
  (``head-row``);
- a ``@cacheable`` method that missed fills the cache: at once outside a unit of work, after the commit of a
  read-only one; so does a cached query of the query bus;
- a lease of the database lock is taken and released.

They used to count: a read-only saga step that failed once was not retried while events were waiting to be
numbered. A write that is the call's own effect still counts (an append to the event store).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest

from pyfly.cache.adapters.postgres import PostgresCacheAdapter
from pyfly.cache.decorators import cacheable
from pyfly.context.application_context import ApplicationContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from pyfly.data.relational.datasource_registry import DataSourceRegistry
from pyfly.data.relational.framework_schema import cache_entries, event_store, event_store_head, locks, snapshots
from pyfly.data.transaction import CommitTracker, track_commits, transactional
from pyfly.eventsourcing.event import StoredEventEnvelope
from pyfly.eventsourcing.store import EventStore
from pyfly.scheduling.adapters.lease_lock import LeaseLock
from pyfly.transactional.saga.annotations import saga, saga_step
from pyfly.transactional.saga.engine.saga_engine import SagaEngine
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

ATTEMPTS: list[int] = []


@saga(name="bookkeeping-read-then-flaky")
class ReadThenFlaky:
    """A read-only step: it reads the global stream, and its first attempt fails after the read."""

    def __init__(self, store: EventStore) -> None:
        self.store = store

    @saga_step(id="read", retry=3, backoff_ms=1)
    async def read(self) -> int:
        page = await self.store.stream_all(after_position=0, limit=10)  # numbers the events waiting for it
        ATTEMPTS.append(len(page))
        if len(ATTEMPTS) == 1:
            raise ConnectionError("the downstream call flaked")
        return len(page)


@dataclass(frozen=True)
class PriceQuery(Query[int]):
    sku: str = "a"


@query_handler(cacheable=True)
class PriceHandler(QueryHandler[PriceQuery, int]):
    async def do_handle(self, query: PriceQuery) -> int:
        return 42


@pytest.fixture
async def context(relational_backend: RelationalBackend) -> AsyncIterator[ApplicationContext]:
    ATTEMPTS.clear()
    await relational_backend.create_tables(event_store, event_store_head, snapshots, cache_entries, locks)
    ctx = ApplicationContext(
        relational_backend.config(
            {
                "pyfly.transactional.enabled": "true",
                "pyfly.eventsourcing.enabled": "true",
                "pyfly.eventsourcing.store.provider": "sqlalchemy",
                "pyfly.eventsourcing.snapshot.provider": "sqlalchemy",
            }
        )
    )
    ctx.register_bean(ReadThenFlaky)
    await ctx.start()
    try:
        yield ctx
    finally:
        await ctx.stop()


def _counts(tracker: CommitTracker) -> tuple[int, int, int]:
    return tracker.committed, tracker.rolled_back, tracker.unknown


async def _append_one(store: EventStore, aggregate_id: str) -> None:
    opened = [StoredEventEnvelope(event_type="Opened", payload={})]
    await store.append(aggregate_id, "Account", opened, expected_version=0)


def _cache(context: ApplicationContext) -> PostgresCacheAdapter:
    return PostgresCacheAdapter(context.get_bean(DataSourceRegistry).primary, create_table=False)


async def test_a_read_of_the_global_stream_that_numbers_events_commits_nothing(context: ApplicationContext) -> None:
    store = context.get_bean(EventStore)
    await _append_one(store, "acc-1")  # committed, waiting for a position

    with track_commits() as commits:
        page = await store.stream_all(after_position=0, limit=10)

    assert [event.global_position for event in page] == [1]  # the read numbered it
    assert _counts(commits) == (0, 0, 0)


async def test_an_append_still_counts(context: ApplicationContext) -> None:
    with track_commits() as commits:
        await _append_one(context.get_bean(EventStore), "acc-1")
    assert _counts(commits) == (1, 0, 0)


async def test_a_cache_fill_after_a_miss_commits_nothing(context: ApplicationContext) -> None:
    cache = _cache(context)
    await cache.start()

    @cacheable(cache, key="price:{sku}")
    async def price(sku: str) -> int:
        return 42

    @transactional(read_only=True)
    async def price_in_a_read_only_unit(sku: str) -> int:
        return await price(sku)

    with track_commits() as commits:
        assert await price("sku-1") == 42  # filled at once
        assert await price_in_a_read_only_unit("sku-2") == 42  # filled once the read-only unit ends

    assert _counts(commits) == (0, 0, 0)
    assert await cache.get("price:sku-1") == 42
    assert await cache.get("price:sku-2") == 42


async def test_a_cached_query_that_missed_commits_nothing(context: ApplicationContext) -> None:
    cache = _cache(context)
    await cache.start()
    registry = HandlerRegistry()
    registry.register_query_handler(PriceHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    alice = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()

    with track_commits() as commits:
        assert await bus.query_with_context(PriceQuery(), alice) == 42

    assert _counts(commits) == (0, 0, 0)
    assert await bus.query_with_context(PriceQuery(), alice) == 42


async def test_a_lease_taken_and_released_commits_nothing(context: ApplicationContext) -> None:
    lock = LeaseLock(context.get_bean(DataSourceRegistry).primary, create_table=False)
    await lock.start()

    with track_commits() as commits:
        assert await lock.try_acquire("nightly-report", ttl=30)
        await lock.release("nightly-report")

    assert _counts(commits) == (0, 0, 0)
    assert await lock.try_acquire("nightly-report", ttl=30)  # it was released
    await lock.release("nightly-report")


async def test_a_read_only_saga_step_that_failed_is_retried_while_events_wait_to_be_numbered(
    context: ApplicationContext,
) -> None:
    await _append_one(context.get_bean(EventStore), "acc-1")

    result = await context.get_bean(SagaEngine).execute("bookkeeping-read-then-flaky")

    assert result.success, result.error
    assert ATTEMPTS == [1, 1]  # the first attempt numbered the event and failed; the second one ran
