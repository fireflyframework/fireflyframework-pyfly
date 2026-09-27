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
"""Clearing the CQRS query cache clears the query cache and nothing else (C034).

The query cache shares the application's ``CacheAdapter`` bean with orchestration state, idempotency
records and ``@cacheable`` entries. ``DefaultQueryBus.clear_all_cache()`` (the documented way to reset
the query cache) used to call the backend's ``clear()``: the whole in-memory store, ``DELETE FROM`` the
PostgreSQL table, ``FLUSHDB`` on Redis.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query


@dataclass(frozen=True)
class CountQuery(Query[int]):
    n: int = 0


@query_handler(cacheable=True)
class CountHandler(QueryHandler[CountQuery, int]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: CountQuery) -> int:
        self.calls += 1
        return query.n


async def test_clear_all_cache_clears_the_query_cache_only() -> None:
    root = InMemoryCache()
    await root.put("orchestration:saga-42", '{"status": "RUNNING"}')
    await root.put("idem:POST:/payments:key-1", {"status": 201})
    await root.put("product:1", {"id": 1})
    registry = HandlerRegistry()
    handler = CountHandler()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(root))

    assert await bus.query(CountQuery(n=3)) == 3
    assert any(key.startswith(":cqrs:") for key in root.get_keys())

    await bus.clear_all_cache()
    assert sorted(root.get_keys()) == ["idem:POST:/payments:key-1", "orchestration:saga-42", "product:1"]
    assert await bus.query(CountQuery(n=3)) == 3
    assert handler.calls == 2


async def test_the_query_cache_adapter_clears_its_prefix_only() -> None:
    root = InMemoryCache()
    adapter = QueryCacheAdapter(root)
    await adapter.put("k", "query result")
    await root.put("orchestration:1", "state")
    await adapter.clear()
    assert root.get_keys() == ["orchestration:1"]
