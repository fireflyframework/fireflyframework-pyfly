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
"""The Redis cache against a real Redis server (WP14: C024, C025, C034).

- C034: the adapter keeps its entries under a namespace (``pyfly:cache:`` by default) and ``clear()``
  deletes that namespace only, never ``FLUSHDB``: sessions, locks and other data in the same database,
  and the caches dedicated to durable consumers (idempotency, orchestration), survive.
- C024: two application instances, each a ``CacheManager`` over the shared Redis with its own in-process
  fallback, both see an eviction the other one makes.
- C025: a JSON hit comes back as the declared return type, and a value Redis cannot hold never fails the
  call that produced it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cache.decorators import cache_evict, cache_put, cacheable
from pyfly.cache.manager import CacheManager
from pyfly.cache.namespaces import dedicated_cache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Any]:
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url)
    await client.flushdb()  # a clean database for this test (the test's own client, not the adapter)
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


async def _keys(client: Any) -> list[str]:
    return sorted(key.decode() for key in await client.keys("*"))


async def test_clear_deletes_the_cache_namespace_only(redis: Any, redis_url: str) -> None:
    await redis.set("pyfly:session:abc", "a user's session")
    await redis.set("unrelated", "another application's data")
    cache = RedisCacheAdapter(redis)
    idempotency = dedicated_cache(cache, "idempotency")
    await cache.put("product:1", {"id": 1})
    await cache.put("product:2", {"id": 2})
    await idempotency.put("idem:POST:/payments:k1", {"status": 201})
    assert await _keys(redis) == [
        "pyfly:cache.idempotency:idem:POST:/payments:k1",
        "pyfly:cache:product:1",
        "pyfly:cache:product:2",
        "pyfly:session:abc",
        "unrelated",
    ]
    assert sorted(await cache.get_keys()) == ["product:1", "product:2"]

    await cache.clear()
    assert await _keys(redis) == [
        "pyfly:cache.idempotency:idem:POST:/payments:k1",
        "pyfly:session:abc",
        "unrelated",
    ]
    assert await idempotency.get("idem:POST:/payments:k1") == {"status": 201}


async def test_prefix_eviction_treats_glob_characters_literally(redis: Any) -> None:
    cache = RedisCacheAdapter(redis)
    await cache.put("a*b", 1)
    await cache.put("a1b", 2)
    await cache.put("[x]", 3)
    assert await cache.evict_by_prefix("a*") == 1
    assert await cache.evict_by_prefix("[") == 1
    assert sorted(await cache.get_keys()) == ["a1b"]


async def test_clearing_the_query_cache_keeps_everything_else(redis: Any) -> None:
    @dataclass(frozen=True)
    class PriceQuery(Query[int]):
        sku: str = ""

    @query_handler(cacheable=True)
    class PriceHandler(QueryHandler[PriceQuery, int]):
        async def do_handle(self, query: PriceQuery) -> int:
            return 10

    cache = RedisCacheAdapter(redis)
    await cache.put("orchestration:saga-42", '{"status": "RUNNING"}')
    await dedicated_cache(cache, "idempotency").put("idem:POST:/payments:k1", {"status": 201})
    registry = HandlerRegistry()
    registry.register_query_handler(PriceHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    assert await bus.query(PriceQuery(sku="x")) == 10
    assert any(key.startswith("pyfly:cache::cqrs:") for key in await _keys(redis))

    await bus.clear_all_cache()
    assert await _keys(redis) == [
        "pyfly:cache.idempotency:idem:POST:/payments:k1",
        "pyfly:cache:orchestration:saga-42",
    ]


async def test_two_instances_see_each_others_evictions(redis_url: str, redis: Any) -> None:
    import redis.asyncio as aioredis

    clients = [aioredis.from_url(redis_url) for _ in range(2)]
    try:
        nodes = [CacheManager(RedisCacheAdapter(client), InMemoryCache()) for client in clients]
        db = {"price": 10}
        loads: list[int] = []

        def reader(node: CacheManager):  # noqa: ANN202
            @cacheable(node, key="price:1")
            async def get_price() -> int:
                loads.append(db["price"])
                return db["price"]

            return get_price

        def writer(node: CacheManager):  # noqa: ANN202
            @cache_evict(node, key="price:1")
            async def set_price(value: int) -> None:
                db["price"] = value

            return set_price

        read_a, read_b = reader(nodes[0]), reader(nodes[1])
        write_a = writer(nodes[0])
        assert await read_b() == 10
        await write_a(11)
        assert await read_b() == 11
        await write_a(12)
        assert [await read_a(), await read_b(), await read_b()] == [12, 12, 12]
        assert loads == [10, 11, 12]
    finally:
        for client in clients:
            await client.aclose()


class InvoiceDto(BaseModel):
    number: str
    total: Decimal
    issued: date


@dataclass
class InvoiceView:
    number: str
    lines: list[str]


async def test_a_json_hit_comes_back_as_the_declared_type(redis: Any) -> None:
    cache = RedisCacheAdapter(redis)
    calls: list[str] = []

    @cacheable(cache, key="invoice:{number}")
    async def get_invoice(number: str) -> InvoiceDto:
        calls.append(number)
        return InvoiceDto(number=number, total=Decimal("12.50"), issued=date(2026, 9, 1))

    @cacheable(cache, key="view:{number}")
    async def get_view(number: str) -> InvoiceView | None:
        calls.append(number)
        return InvoiceView(number=number, lines=["a", "b"])

    first = await get_invoice("F-1")
    hit = await get_invoice("F-1")
    assert isinstance(hit, InvoiceDto)
    assert hit == first
    view = await get_view("F-2")
    assert await get_view("F-2") == view
    assert calls == ["F-1", "F-2"]


async def test_a_value_redis_cannot_hold_never_fails_the_call(redis: Any, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    class Opaque:
        pass

    cache = RedisCacheAdapter(redis)
    writes: list[str] = []

    @cache_put(cache, key="opaque:{n}")
    async def create(n: int):  # noqa: ANN202 — unannotated on purpose: refused at run time
        writes.append("row")
        return Opaque()

    caplog.set_level(logging.WARNING, logger="pyfly.cache")
    assert isinstance(await create(1), Opaque)
    assert writes == ["row"]
    assert await _keys(redis) == []
    assert any(record.getMessage().startswith("cache_put_skipped") for record in caplog.records)
