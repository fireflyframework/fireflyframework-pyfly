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
"""A ``None`` query result is cached only when the handler opts in (C165).

``DefaultQueryBus`` used to store every ``None`` result and never serve it back, so a lookup for a
missing row paid the handler's query plus a cache write on every call. Now a ``None`` result is not stored
unless the handler declares ``cache_none``, and then it is served from the cache like any other result.
The handler runs a real ``SELECT`` on a SQLite file database and on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query
from pyfly.testing.statement_counter import StatementCounter
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

CALLER = ExecutionContextBuilder().with_tenant_id("acme").with_user_id("alice").build()
"""Who runs the queries: the query cache caches a call only when it can see who the caller is."""


@dataclass(frozen=True)
class FindUserByEmail(Query[str | None]):
    email: str = ""


@dataclass(frozen=True)
class FindAccountByEmail(Query[str | None]):
    email: str = ""


async def _lookup(engine: AsyncEngine, email: str) -> str | None:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT name FROM wp14_users WHERE email = :e"), {"e": email})).first()
        return None if row is None else str(row[0])


@pytest.fixture
async def world(relational_backend: RelationalBackend) -> AsyncIterator[dict[str, Any]]:
    engine = relational_backend.create_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE wp14_users (id INTEGER PRIMARY KEY, email VARCHAR(64), name VARCHAR(64))")
        )
        await conn.execute(text("INSERT INTO wp14_users (id, email, name) VALUES (1, 'ann@example.com', 'Ann')"))

    @query_handler(cacheable=True)
    class FindUserHandler(QueryHandler[FindUserByEmail, str | None]):
        async def do_handle(self, query: FindUserByEmail) -> str | None:
            return await _lookup(engine, query.email)

    @cacheable(cache_none=True)
    @query_handler(cacheable=True)
    class FindAccountHandler(QueryHandler[FindAccountByEmail, str | None]):
        async def do_handle(self, query: FindAccountByEmail) -> str | None:
            return await _lookup(engine, query.email)

    registry = HandlerRegistry()
    registry.register_query_handler(FindUserHandler())
    registry.register_query_handler(FindAccountHandler())
    cache = InMemoryCache()
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    yield {"bus": bus, "engine": engine, "cache": cache}


async def test_a_none_result_is_not_stored_by_default(world: dict[str, Any]) -> None:
    bus: DefaultQueryBus = world["bus"]
    with StatementCounter(world["engine"]) as counter:
        for _ in range(3):
            assert await bus.query_with_context(FindUserByEmail(email="nobody@example.com"), CALLER) is None
    assert counter.count("SELECT") == 3  # every call asks the database: nothing was cached
    # No write was wasted on an entry never read back (the key's generation is all the lookups stored).
    assert [key for key in world["cache"].get_keys() if not key.endswith("|generation")] == []


async def test_a_handler_that_opts_in_is_served_its_cached_none(world: dict[str, Any]) -> None:
    bus: DefaultQueryBus = world["bus"]
    with StatementCounter(world["engine"]) as counter:
        for _ in range(3):
            assert await bus.query_with_context(FindAccountByEmail(email="nobody@example.com"), CALLER) is None
        assert await bus.query_with_context(FindAccountByEmail(email="ann@example.com"), CALLER) == "Ann"
        assert await bus.query_with_context(FindAccountByEmail(email="ann@example.com"), CALLER) == "Ann"
    assert counter.count("SELECT") == 2


async def test_a_found_row_is_cached_as_before(world: dict[str, Any]) -> None:
    bus: DefaultQueryBus = world["bus"]
    with StatementCounter(world["engine"]) as counter:
        for _ in range(3):
            assert await bus.query_with_context(FindUserByEmail(email="ann@example.com"), CALLER) == "Ann"
    assert counter.count("SELECT") == 1
