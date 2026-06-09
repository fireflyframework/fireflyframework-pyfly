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
"""Item 3 — EDA → CQRS cache-invalidation bridge end-to-end tests.

Uses REAL in-memory EDA (InMemoryEventBus) and REAL InMemoryCache.
No Docker required.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import CQRS_CACHE_PREFIX, QueryCacheAdapter
from pyfly.cqrs.cache.eda_bridge import EdaCacheInvalidationBridge
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from pyfly.eda.adapters.memory import InMemoryEventBus

# ---------------------------------------------------------------------------
# Query / handler fixtures for the end-to-end bridge test
# ---------------------------------------------------------------------------


@dataclass
class GetWidgetQuery(Query[dict]):
    """Cacheable query — cache key encodes the widget_id."""

    widget_id: str = ""

    def get_cache_key(self) -> str:
        return f"widget:{self.widget_id}"


@query_handler(cacheable=True, cache_ttl=300)
class GetWidgetHandler(QueryHandler[GetWidgetQuery, dict]):
    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    async def do_handle(self, query: GetWidgetQuery) -> dict:
        self.call_count += 1
        return {"id": query.widget_id, "name": "Sprocket"}


class TestEdaCacheInvalidationBridge:
    """End-to-end tests: bridge subscribes to a real InMemoryEventBus."""

    @pytest.fixture
    async def setup(self) -> tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus]:
        mem_cache = InMemoryCache()
        await mem_cache.start()
        cache_adapter = QueryCacheAdapter(cache=mem_cache)

        bus = InMemoryEventBus()
        await bus.start()

        bridge = EdaCacheInvalidationBridge(cache_adapter)
        return cache_adapter, bridge, bus

    async def test_matching_event_evicts_cache_key(
        self,
        setup: tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus],
    ) -> None:
        cache_adapter, bridge, bus = setup

        # Pre-populate the cache with a value for key "order:42".
        await cache_adapter.put("order:42", {"status": "pending"})
        assert await cache_adapter.get("order:42") is not None

        # Register rule and subscribe bridge to the EDA bus.
        bridge.register("order.updated", "order:{order_id}")
        bridge.subscribe(bus)

        # Publish the event — the bridge should evict "order:42".
        await bus.publish("cqrs.events", "order.updated", {"order_id": "42"})

        # The cache key must now be gone.
        assert await cache_adapter.get("order:42") is None

    async def test_non_matching_event_does_not_evict(
        self,
        setup: tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus],
    ) -> None:
        cache_adapter, bridge, bus = setup

        await cache_adapter.put("order:99", {"status": "shipped"})
        bridge.register("order.updated", "order:{order_id}")
        bridge.subscribe(bus)

        # Publish a different event type — the rule should NOT fire.
        await bus.publish("cqrs.events", "order.deleted", {"order_id": "99"})

        # The value for "order:99" should still be present.
        assert await cache_adapter.get("order:99") is not None

    async def test_multiple_rules_same_event_type_all_evicted(
        self,
        setup: tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus],
    ) -> None:
        cache_adapter, bridge, bus = setup

        await cache_adapter.put("order:7", {"status": "new"})
        await cache_adapter.put("customer-orders:7", {"count": 3})

        bridge.register("order.updated", "order:{order_id}")
        bridge.register("order.updated", "customer-orders:{order_id}")
        bridge.subscribe(bus)

        await bus.publish("cqrs.events", "order.updated", {"order_id": "7"})

        assert await cache_adapter.get("order:7") is None
        assert await cache_adapter.get("customer-orders:7") is None

    async def test_missing_payload_field_leaves_placeholder_intact(
        self,
        setup: tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus],
    ) -> None:
        cache_adapter, bridge, bus = setup

        # Put a cache entry with the literal placeholder in its key — this is
        # the fallback behaviour when the field is absent from the payload.
        prefixed_literal = f"{CQRS_CACHE_PREFIX}order:{{order_id}}"
        await cache_adapter._cache.put(prefixed_literal, {"bogus": True})  # type: ignore[union-attr]

        bridge.register("order.updated", "order:{order_id}")
        bridge.subscribe(bus)

        # Publish without the expected field — should log a warning and not crash.
        await bus.publish("cqrs.events", "order.updated", {"customer_id": "99"})
        # The literal-placeholder key was "evicted" (or not), but the test
        # merely asserts no exception was raised.

    async def test_bridge_noop_when_no_rules_registered(
        self,
        setup: tuple[QueryCacheAdapter, EdaCacheInvalidationBridge, InMemoryEventBus],
    ) -> None:
        cache_adapter, bridge, bus = setup

        await cache_adapter.put("order:1", "v")
        bridge.subscribe(bus)  # no rules registered

        await bus.publish("cqrs.events", "order.updated", {"order_id": "1"})

        # Nothing evicted because no rules exist.
        assert await cache_adapter.get("order:1") is not None


class TestEdaCacheInvalidationBridgeResolution:
    """Unit tests for the pattern-resolution helper."""

    def _bridge(self) -> EdaCacheInvalidationBridge:
        return EdaCacheInvalidationBridge(QueryCacheAdapter(None))

    def test_simple_placeholder_resolved(self) -> None:
        result = EdaCacheInvalidationBridge._resolve_pattern("order:{order_id}", {"order_id": "42"})
        assert result == "order:42"

    def test_multiple_placeholders_resolved(self) -> None:
        result = EdaCacheInvalidationBridge._resolve_pattern(
            "tenant:{tenant_id}:order:{order_id}",
            {"tenant_id": "acme", "order_id": "7"},
        )
        assert result == "tenant:acme:order:7"

    def test_missing_field_leaves_placeholder(self) -> None:
        result = EdaCacheInvalidationBridge._resolve_pattern("order:{order_id}", {})
        assert result == "order:{order_id}"

    def test_no_placeholders_unchanged(self) -> None:
        result = EdaCacheInvalidationBridge._resolve_pattern("orders:all", {"order_id": "1"})
        assert result == "orders:all"


# ---------------------------------------------------------------------------
# End-to-end test: bridge evicts entries that were cached THROUGH the bus
# ---------------------------------------------------------------------------


class TestEdaBridgeEvictsBusCachedEntries:
    """Proves that the EDA bridge correctly evicts entries stored by the bus.

    This is the regression test for the double-prefix bug:
    - Before the fix: bus stored at ``:cqrs::cqrs:{key}``, bridge evicted
      ``:cqrs:{key}`` → mismatch → entries were never invalidated.
    - After the fix: bus passes the raw key to ``QueryCacheAdapter``; the adapter
      applies a single ``:cqrs:`` prefix → bus stores at ``:cqrs:{key}``.
      The bridge also evicts via the same adapter, so it targets ``:cqrs:{key}``
      → the eviction succeeds and the bus re-executes the handler.
    """

    async def test_eda_event_evicts_bus_cached_entry(self) -> None:
        """Bus-cached query result is evicted when the matching EDA event fires."""
        # Wire everything up with real in-memory adapters.
        mem_cache = InMemoryCache()
        await mem_cache.start()
        cache_adapter = QueryCacheAdapter(cache=mem_cache)

        registry = HandlerRegistry()
        handler = GetWidgetHandler()
        registry.register_query_handler(handler)

        bus = DefaultQueryBus(registry=registry, cache_adapter=cache_adapter)

        eda_bus = InMemoryEventBus()
        await eda_bus.start()

        bridge = EdaCacheInvalidationBridge(cache_adapter)
        # The bridge rule must match the raw cache-key pattern that the query
        # produces.  GetWidgetQuery.get_cache_key() returns "widget:{widget_id}".
        bridge.register("widget.updated", "widget:{widget_id}")
        bridge.subscribe(eda_bus)

        # Step 1 — run the query so the result is cached through the bus.
        result1 = await bus.query(GetWidgetQuery(widget_id="w-99"))
        assert result1 == {"id": "w-99", "name": "Sprocket"}
        assert handler.call_count == 1

        # Step 2 — confirm the cache hit (handler NOT called again).
        result2 = await bus.query(GetWidgetQuery(widget_id="w-99"))
        assert result2 == {"id": "w-99", "name": "Sprocket"}
        assert handler.call_count == 1  # served from cache

        # Step 3 — fire the invalidating EDA event.
        await eda_bus.publish("cqrs.events", "widget.updated", {"widget_id": "w-99"})

        # Step 4 — the cache entry must be gone; next query re-executes the handler.
        result3 = await bus.query(GetWidgetQuery(widget_id="w-99"))
        assert result3 == {"id": "w-99", "name": "Sprocket"}
        assert handler.call_count == 2, (
            "Handler must be called again after EDA invalidation — bridge failed to evict the bus-cached entry"
        )
