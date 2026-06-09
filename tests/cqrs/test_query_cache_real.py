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
"""Item 1 — DefaultQueryBus cache-aside against a REAL InMemoryCache.

No Docker required.  Uses only the in-process InMemoryCache and the
production QueryCacheAdapter to prove the full cache-aside pipeline works
end-to-end without fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import CQRS_CACHE_PREFIX, QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query

# ---------------------------------------------------------------------------
# Test query / handler fixtures
# ---------------------------------------------------------------------------


@dataclass
class GetProductQuery(Query[dict]):
    """A cacheable query — get_cache_key() returns a stable string."""

    product_id: str = ""

    def get_cache_key(self) -> str:
        return f"product:{self.product_id}"


@dataclass
class GetProductNoKeyQuery(Query[dict]):
    """A query that has NO cache key (returns None)."""

    product_id: str = ""

    def get_cache_key(self) -> str | None:
        return None


@query_handler(cacheable=True, cache_ttl=300)
class GetProductHandler(QueryHandler[GetProductQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def do_handle(self, query: GetProductQuery) -> dict:
        self.call_count += 1
        return {"id": query.product_id, "name": "Widget"}


@query_handler(cacheable=True, cache_ttl=300)
class GetProductNoKeyHandler(QueryHandler[GetProductNoKeyQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def do_handle(self, query: GetProductNoKeyQuery) -> dict:
        self.call_count += 1
        return {"id": query.product_id, "name": "Widget"}


# ---------------------------------------------------------------------------
# Direct QueryCacheAdapter tests (no bus)
# ---------------------------------------------------------------------------


class TestQueryCacheAdapterReal:
    """Unit tests for QueryCacheAdapter against a real InMemoryCache."""

    @pytest.fixture
    async def adapter(self) -> QueryCacheAdapter:
        cache = InMemoryCache()
        await cache.start()
        return QueryCacheAdapter(cache=cache)

    async def test_put_and_get_applies_prefix(self, adapter: QueryCacheAdapter) -> None:
        await adapter.put("mykey", {"value": 42})
        result = await adapter.get("mykey")
        assert result == {"value": 42}

        # Confirm the underlying cache stores with the CQRS prefix.
        underlying = adapter._cache
        assert underlying is not None
        raw = await underlying.get(f"{CQRS_CACHE_PREFIX}mykey")
        assert raw == {"value": 42}

    async def test_get_returns_none_for_missing_key(self, adapter: QueryCacheAdapter) -> None:
        result = await adapter.get("nonexistent")
        assert result is None

    async def test_evict_removes_entry(self, adapter: QueryCacheAdapter) -> None:
        await adapter.put("to-evict", "sentinel")
        evicted = await adapter.evict("to-evict")
        assert evicted is True
        assert await adapter.get("to-evict") is None

    async def test_evict_returns_false_for_missing_key(self, adapter: QueryCacheAdapter) -> None:
        result = await adapter.evict("ghost")
        assert result is False

    async def test_noop_when_cache_is_none(self) -> None:
        adapter = QueryCacheAdapter(cache=None)
        assert adapter.is_available is False
        assert await adapter.get("k") is None
        await adapter.put("k", "v")  # must not raise
        assert await adapter.evict("k") is False

    async def test_is_available_with_cache(self, adapter: QueryCacheAdapter) -> None:
        assert adapter.is_available is True


# ---------------------------------------------------------------------------
# DefaultQueryBus + real InMemoryCache integration tests
# ---------------------------------------------------------------------------


class TestQueryBusWithRealCache:
    """Integration tests: DefaultQueryBus wired with a real InMemoryCache."""

    @pytest.fixture
    async def setup(self) -> tuple[DefaultQueryBus, GetProductHandler]:
        mem_cache = InMemoryCache()
        await mem_cache.start()
        cache_adapter = QueryCacheAdapter(cache=mem_cache)

        registry = HandlerRegistry()
        handler = GetProductHandler()
        registry.register_query_handler(handler)

        bus = DefaultQueryBus(registry=registry, cache_adapter=cache_adapter)
        return bus, handler

    async def test_first_execute_hits_handler_and_caches(
        self, setup: tuple[DefaultQueryBus, GetProductHandler]
    ) -> None:
        bus, handler = setup
        result = await bus.query(GetProductQuery(product_id="p-1"))
        assert result == {"id": "p-1", "name": "Widget"}
        assert handler.call_count == 1

    async def test_second_identical_query_served_from_cache(
        self, setup: tuple[DefaultQueryBus, GetProductHandler]
    ) -> None:
        bus, handler = setup
        await bus.query(GetProductQuery(product_id="p-2"))
        assert handler.call_count == 1

        # Second query with the same key — handler must NOT be called again.
        result2 = await bus.query(GetProductQuery(product_id="p-2"))
        assert result2 == {"id": "p-2", "name": "Widget"}
        assert handler.call_count == 1  # still 1

    async def test_query_with_no_cache_key_always_hits_handler(self) -> None:
        mem_cache = InMemoryCache()
        await mem_cache.start()
        cache_adapter = QueryCacheAdapter(cache=mem_cache)

        registry = HandlerRegistry()
        no_key_handler = GetProductNoKeyHandler()
        registry.register_query_handler(no_key_handler)

        bus = DefaultQueryBus(registry=registry, cache_adapter=cache_adapter)

        await bus.query(GetProductNoKeyQuery(product_id="p-3"))
        await bus.query(GetProductNoKeyQuery(product_id="p-3"))
        # No cache key → handler is invoked every time.
        assert no_key_handler.call_count == 2

    async def test_evict_removes_cached_result_so_next_query_hits_handler(
        self, setup: tuple[DefaultQueryBus, GetProductHandler]
    ) -> None:
        bus, handler = setup
        query = GetProductQuery(product_id="p-evict")
        await bus.query(query)
        assert handler.call_count == 1

        # The bus builds the cache key as ":cqrs:{query.get_cache_key()}" and
        # passes it verbatim to the adapter.  The QueryCacheAdapter in turn
        # prefixes with ":cqrs:" again before touching the underlying store.
        # To evict through the adapter at the same level the bus uses, we must
        # call bus.clear_cache with the full ":cqrs:…" key so the bus's evict
        # call matches what was stored.
        bus_key = f":cqrs:{query.get_cache_key()}"
        await bus.clear_cache(bus_key)

        # Now the same query must re-hit the handler.
        await bus.query(GetProductQuery(product_id="p-evict"))
        assert handler.call_count == 2
