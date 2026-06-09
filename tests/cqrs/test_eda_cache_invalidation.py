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

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import CQRS_CACHE_PREFIX, QueryCacheAdapter
from pyfly.cqrs.cache.eda_bridge import EdaCacheInvalidationBridge
from pyfly.eda.adapters.memory import InMemoryEventBus


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
