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
"""The generations of scoped query-cache keys expire with the entries they serve.

A scoped entry lives under its key's current generation (``<key>|<generation>|scope=<digest>``); the
generation is itself an entry (``<key>|generation``). Every distinct query key looked up by a scoped
caller creates one, and the first lookup after an eviction (which deletes it) creates a fresh one: without
a TTL they outlive their entries forever, an unbounded leak in the default in-memory cache and keys that a
volatile-* Redis maxmemory policy can never evict. An eviction itself writes nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import GENERATION_SUFFIX, QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query


@dataclass(frozen=True)
class GetStock(Query[int]):
    sku: int = 0


@cacheable(ttl=1)
@query_handler(cacheable=True)
class GetStockHandler(QueryHandler[GetStock, int]):
    async def do_handle(self, query: GetStock) -> int:
        return query.sku


async def test_no_query_cache_key_outlives_the_ttl_of_its_entries() -> None:
    root = InMemoryCache()
    registry = HandlerRegistry()
    registry.register_query_handler(GetStockHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(root))
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()

    for sku in range(50):
        await bus.query_with_context(GetStock(sku=sku), context)
    for sku in range(0, 50, 5):
        await bus.clear_cache(GetStock(sku=sku).get_cache_key())  # deletes those keys' generations
    assert any(key.endswith(GENERATION_SUFFIX) for key in root.get_keys())

    await asyncio.sleep(1.1)
    assert root.get_keys() == []


async def test_a_generation_expires_with_the_ttl_of_its_entries() -> None:
    root = InMemoryCache()
    adapter = QueryCacheAdapter(root)
    key = await adapter.entry_key("Report:1", "digest", ttl=timedelta(milliseconds=50))
    assert key is not None
    await adapter.put(key, "result", ttl=timedelta(milliseconds=50))
    assert ":cqrs:Report:1|generation" in root.get_keys()

    await asyncio.sleep(0.1)
    assert root.get_keys() == []


class CountingCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    async def put(self, key: str, value: object, ttl: timedelta | None = None) -> None:
        self.writes.append(key)
        await super().put(key, value, ttl=ttl)

    async def put_if_absent(self, key: str, value: object, ttl: timedelta | None = None) -> bool:
        self.writes.append(key)
        return await super().put_if_absent(key, value, ttl=ttl)


async def test_an_eviction_deletes_the_generation_and_writes_nothing() -> None:
    root = CountingCache()
    adapter = QueryCacheAdapter(root)
    key = await adapter.entry_key("Report:1", "digest", ttl=timedelta(seconds=60))
    assert key is not None
    await adapter.put(key, "result", ttl=timedelta(seconds=60))
    root.writes.clear()

    assert await adapter.evict("Report:1") is True
    assert await adapter.evict("Report:2") is False  # never cached
    assert root.writes == []
    assert root.get_keys() == [":cqrs:" + key]  # unreachable now, it expires with its TTL
    after = await adapter.entry_key("Report:1", "digest", ttl=timedelta(seconds=60))
    assert after is not None and after != key
    assert await adapter.lookup(after) == (False, None)


async def test_an_expired_generation_never_brings_back_an_evicted_entry() -> None:
    root = InMemoryCache()
    adapter = QueryCacheAdapter(root)
    ttl = timedelta(milliseconds=50)
    before = await adapter.entry_key("Report:1", "digest", ttl=ttl)
    assert before is not None
    await adapter.put(before, "stale", ttl=timedelta(seconds=60))  # outlives its generation
    await adapter.evict("Report:1")

    await asyncio.sleep(0.1)  # the new generation expired too: the next lookup starts a fresh one
    after = await adapter.entry_key("Report:1", "digest", ttl=ttl)
    assert after is not None and after != before
    assert await adapter.lookup(after) == (False, None)
