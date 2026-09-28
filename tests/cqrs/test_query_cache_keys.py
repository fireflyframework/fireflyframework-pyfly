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
"""Query-cache keys: what a key may contain, and what a lookup costs.

Unscoped entries live under the query's own key, scoped ones under ``<key>|<generation>|scope=<digest>``,
and a key's generation under ``<key>|generation``: one keyspace. A query key built from user input that
contains ``|scope=`` or ends with ``|generation`` could otherwise address another scope's entry, or read or
overwrite a key's generation, so such a key is never cached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query, QueryCacheScope


@dataclass(frozen=True)
class PrivateReport(Query[str]):
    report: str = ""

    def get_cache_key(self) -> str | None:
        return f"report:{self.report}"


@dataclass(frozen=True)
class Lookup(Query[Any]):
    """A GLOBAL query whose key is whatever the caller sends."""

    raw_key: str = ""

    def get_cache_key(self) -> str | None:
        return self.raw_key


@query_handler(cacheable=True)
class PrivateReportHandler(QueryHandler[PrivateReport, str]):
    async def do_handle(self, query: PrivateReport) -> str:
        return f"{query.report} for its owner only"


@cacheable(scope=QueryCacheScope.GLOBAL)
@query_handler(cacheable=True)
class LookupHandler(QueryHandler[Lookup, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: Lookup) -> Any:
        self.calls += 1
        return "public"


class CountingCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.exists_calls = 0

    async def exists(self, key: str) -> bool:
        self.exists_calls += 1
        return await super().exists(key)


def _bus(cache: InMemoryCache) -> tuple[DefaultQueryBus, LookupHandler]:
    registry = HandlerRegistry()
    lookup = LookupHandler()
    registry.register_query_handler(PrivateReportHandler())
    registry.register_query_handler(lookup)
    return DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache)), lookup


async def test_a_raw_key_can_address_neither_a_scoped_entry_nor_a_generation(caplog: pytest.LogCaptureFixture) -> None:
    root = InMemoryCache()
    bus, lookup = _bus(root)
    alice = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
    assert await bus.query_with_context(PrivateReport(report="salaries"), alice) == "salaries for its owner only"
    keys = sorted(key.removeprefix(":cqrs:") for key in root.get_keys())
    [generation_key] = [key for key in keys if key.endswith("|generation")]
    [entry_key] = [key for key in keys if "|scope=" in key]

    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.cache.adapter")
    # Another caller sends the exact keys: the GLOBAL handler runs and its result is not cached under them.
    assert await bus.query(Lookup(raw_key=entry_key)) == "public"
    assert await bus.query(Lookup(raw_key=generation_key)) == "public"
    assert await bus.query(Lookup(raw_key=entry_key)) == "public"
    assert lookup.calls == 3
    assert await bus.query_with_context(PrivateReport(report="salaries"), alice) == "salaries for its owner only"
    assert sorted(key.removeprefix(":cqrs:") for key in root.get_keys()) == keys
    assert len(caplog.records) == 1 and "never cached" in caplog.records[0].getMessage()


async def test_a_miss_costs_no_existence_check_when_none_is_not_cached() -> None:
    root = CountingCache()
    bus, lookup = _bus(root)
    for _ in range(2):
        assert await bus.query(Lookup(raw_key="k")) == "public"
    assert lookup.calls == 1
    assert root.exists_calls == 0  # the handler does not cache None: a None entry cannot be a hit
