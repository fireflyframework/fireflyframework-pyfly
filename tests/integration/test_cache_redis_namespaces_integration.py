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
- C025: a JSON hit comes back as the declared return type (Pydantic models with aliases or computed
  fields included, through the decorators and the query bus), and a value Redis cannot hold never fails
  the call that produced it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, computed_field
from pydantic.alias_generators import to_camel

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cache.adapters.redis import RedisCacheAdapter
from pyfly.cache.decorators import cache_evict, cache_put, cacheable
from pyfly.cache.manager import CacheManager
from pyfly.cache.namespaces import dedicated_cache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cacheable as query_cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query, QueryCacheScope


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


async def test_clearing_a_custom_namespace_keeps_the_ones_that_start_like_it(redis: Any) -> None:
    cache = RedisCacheAdapter(redis, namespace="myapp")
    await cache.put("product:1", {"id": 1})
    await cache.with_namespace("idempotency").put("idem:k1", {"status": 201})
    await RedisCacheAdapter(redis, namespace="myapp2").put("product:1", {"id": 2})

    await cache.clear()
    assert await _keys(redis) == ["myapp.idempotency:idem:k1", "myapp2:product:1"]


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

    @query_cacheable(scope=QueryCacheScope.GLOBAL)  # a list price is the same for every caller
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


async def test_every_query_cache_key_expires(redis: Any) -> None:
    @dataclass(frozen=True)
    class StockQuery(Query[int]):
        sku: int = 0

    @query_handler(cacheable=True, cache_ttl=120)
    class StockHandler(QueryHandler[StockQuery, int]):
        async def do_handle(self, query: StockQuery) -> int:
            return query.sku

    registry = HandlerRegistry()
    registry.register_query_handler(StockHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(RedisCacheAdapter(redis)))
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
    for sku in range(5):
        await bus.query_with_context(StockQuery(sku=sku), context)
    await bus.clear_cache(StockQuery(sku=0).get_cache_key())  # that key's generation is deleted

    keys = [key for key in await _keys(redis) if key.startswith("pyfly:cache::cqrs:")]
    assert any(key.endswith("|generation") for key in keys)
    ttls = {key: await redis.ttl(key) for key in keys}
    # -1 is a key without an expiry: none may outlive the entries it serves.
    assert all(0 < ttl <= 120 for ttl in ttls.values()), ttls


async def test_evicting_a_query_key_deletes_and_writes_nothing(redis: Any) -> None:
    """A command's ``get_cache_key()`` eviction: one step of concurrent deletes (each key's unscoped entry
    and generation, under every ``cache_key_prefix``), and not a single write, whether the key was ever
    cached or not."""

    @dataclass(frozen=True)
    class StockQuery(Query[int]):
        sku: int = 0

        def get_cache_key(self) -> str | None:
            return f"stock:{self.sku}"

    def prefixed_handler(n: int) -> QueryHandler[Any, int]:
        @dataclass(frozen=True)
        class PrefixedStockQuery(StockQuery):
            pass

        @query_cacheable(cache_key_prefix=f"p{n}")
        @query_handler(cacheable=True)
        class PrefixedStockHandler(QueryHandler[PrefixedStockQuery, int]):
            async def do_handle(self, query: PrefixedStockQuery) -> int:
                return query.sku

        return PrefixedStockHandler()

    registry = HandlerRegistry()
    handlers = [prefixed_handler(n) for n in range(5)]
    for handler in handlers:
        registry.register_query_handler(handler)
    cache = RedisCacheAdapter(redis)
    queries = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
    await queries.query_with_context(handlers[0].get_query_type()(sku=1), context)
    before = await _keys(redis)
    assert any(key.endswith("p0:stock:1|generation") for key in before)

    commands: list[tuple[Any, ...]] = []
    execute = redis.execute_command

    async def recorded(*args: Any, **kwargs: Any) -> Any:
        commands.append(args)
        return await execute(*args, **kwargs)

    redis.execute_command = recorded
    try:
        await queries.clear_cache("stock:1")  # cached under p0 only
        await queries.clear_cache("stock:2")  # never cached
    finally:
        redis.execute_command = execute

    names = [str(command[0]).upper() for command in commands]
    assert set(names) == {"DEL"}, names  # no SET: nothing is written for a key never cached
    assert len(names) == 2 * 2 * 6  # two keys, each its entry and generation, bare and under five prefixes
    left = [key for key in await _keys(redis) if "stock:1" in key]
    assert left and all("|scope=" in key for key in left)  # unreachable (its generation is gone) until its TTL


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


class MemberDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)

    member_id: int
    display_name: str


class ShipmentDto(BaseModel):
    tracking_no: str = Field(alias="trackingNo")


class LineDto(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int
    unit_price: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> Decimal:
        return self.quantity * self.unit_price


async def test_aliased_and_computed_dto_hits_come_back_from_redis(redis: Any, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    cache = RedisCacheAdapter(redis)
    calls: list[str] = []

    @cacheable(cache, key="member:{n}")
    async def get_member(n: int) -> MemberDto:
        calls.append("member")
        return MemberDto(memberId=n, displayName="Ada")

    @cacheable(cache, key="shipment:{n}")
    async def get_shipment(n: str) -> ShipmentDto:
        calls.append("shipment")
        return ShipmentDto(trackingNo=n)

    @cacheable(cache, key="lines:{n}")
    async def get_lines(n: int) -> list[LineDto]:
        calls.append("lines")
        return [LineDto(quantity=n, unit_price=Decimal("1.50"))]

    caplog.set_level(logging.WARNING, logger="pyfly.cache")
    for _ in range(3):
        assert await get_member(1) == MemberDto(memberId=1, displayName="Ada")
        assert await get_shipment("Z-9") == ShipmentDto(trackingNo="Z-9")
        assert await get_lines(2) == [LineDto(quantity=2, unit_price=Decimal("1.50"))]
    assert calls == ["member", "shipment", "lines"]
    assert caplog.records == []


async def test_a_query_bus_hit_of_an_aliased_dto_comes_back_from_redis(
    redis: Any, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    @dataclass(frozen=True)
    class GetMember(Query[MemberDto]):
        member_id: int = 0

    @query_handler(cacheable=True)
    class GetMemberHandler(QueryHandler[GetMember, MemberDto]):
        calls = 0

        async def do_handle(self, query: GetMember) -> MemberDto:
            type(self).calls += 1
            return MemberDto(memberId=query.member_id, displayName="Ada")

    registry = HandlerRegistry()
    registry.register_query_handler(GetMemberHandler())
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(RedisCacheAdapter(redis)))
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()

    caplog.set_level(logging.WARNING)
    results = [await bus.query_with_context(GetMember(member_id=5), context) for _ in range(3)]
    assert results == [MemberDto(memberId=5, displayName="Ada")] * 3
    assert all(isinstance(result, MemberDto) for result in results)
    assert GetMemberHandler.calls == 1
    assert caplog.records == []


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
