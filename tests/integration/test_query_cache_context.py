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
"""A cached query result never crosses tenants or users (C001, critical).

The query cache key used to be the query's own key only, so the first tenant (or user) to run a
cacheable query filled the entry every other tenant was then served for ``cache_ttl``. The key now
carries the caller's scope: the ``ExecutionContext`` of ``query_with_context`` (tenant, organization,
user), or the ambient tenant (``X-Tenant-Id``) and authenticated user of a plain ``query``.

The handler reads real rows scoped by tenant from a SQLite file database (foreign keys on) and from
PostgreSQL, as a multi-tenant service does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.context.request_context import RequestContext
from pyfly.cqrs.cache.adapter import QueryCacheAdapter
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContext, ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.exceptions import QueryProcessingException
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import ContextAwareQueryHandler, QueryHandler
from pyfly.cqrs.types import Query, QueryCacheScope
from pyfly.observability import correlation
from pyfly.security.context import SecurityContext
from tests.support.backend_matrix import PG, SQLITE_FILE, RelationalBackend

pytestmark = pytest.mark.backends(SQLITE_FILE, PG)

Setup = tuple[DefaultQueryBus, dict[str, Any], InMemoryCache]


@dataclass(frozen=True)
class ListDocsQuery(Query[list[str]]):
    status: str = "open"


@dataclass(frozen=True)
class ListMyDocsQuery(Query[list[str]]):
    status: str = "open"


@dataclass(frozen=True)
class CountriesQuery(Query[list[str]]):
    pass


async def _docs(engine: AsyncEngine, tenant: str | None, owner: str | None = None) -> list[str]:
    sql = "SELECT tenant || ': ' || title FROM wp14_docs WHERE tenant = :tenant"
    params = {"tenant": tenant}
    if owner is not None:
        sql += " AND owner = :owner"
        params["owner"] = owner
    async with engine.connect() as conn:
        return [row[0] for row in (await conn.execute(text(sql + " ORDER BY id"), params)).all()]


def _make_handlers(engine: AsyncEngine) -> dict[str, Any]:
    @query_handler(cacheable=True)
    class ListDocsHandler(ContextAwareQueryHandler[ListDocsQuery, list[str]]):
        calls = 0

        async def do_handle_with_context(self, query: ListDocsQuery, context: ExecutionContext) -> list[str]:
            type(self).calls += 1
            return await _docs(engine, context.tenant_id)

    @query_handler(cacheable=True)
    class ListMyDocsHandler(QueryHandler[ListMyDocsQuery, list[str]]):
        """Scopes by the ambient tenant and user, as a service behind the web filters does."""

        calls = 0

        async def do_handle(self, query: ListMyDocsQuery) -> list[str]:
            type(self).calls += 1
            request = RequestContext.current()
            user = request.security_context.user_id if request and request.security_context else None
            return await _docs(engine, correlation.get_tenant_id(), user)

    @cacheable(scope=QueryCacheScope.GLOBAL)
    @query_handler(cacheable=True)
    class CountriesHandler(QueryHandler[CountriesQuery, list[str]]):
        calls = 0

        async def do_handle(self, query: CountriesQuery) -> list[str]:
            type(self).calls += 1
            return ["ES", "US"]

    return {"docs": ListDocsHandler(), "mine": ListMyDocsHandler(), "countries": CountriesHandler()}


@pytest.fixture
async def setup(relational_backend: RelationalBackend) -> AsyncIterator[Setup]:
    engine = relational_backend.create_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE wp14_docs (id INTEGER PRIMARY KEY, tenant VARCHAR(32), owner VARCHAR(32), "
                "title VARCHAR(64))"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO wp14_docs (id, tenant, owner, title) VALUES (1, 'acme', 'alice', 'Q3 payroll'), "
                "(2, 'acme', 'bob', 'bonus plan'), (3, 'globex', 'carol', 'widgets')"
            )
        )
    handlers = _make_handlers(engine)
    registry = HandlerRegistry()
    for handler in handlers.values():
        registry.register_query_handler(handler)
    cache = InMemoryCache()
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache), default_cache_ttl=900)
    yield bus, handlers, cache


def _ctx(tenant: str, user: str | None = None) -> ExecutionContext:
    builder = ExecutionContextBuilder().with_tenant_id(tenant)
    if user is not None:
        builder = builder.with_user_id(user)
    return builder.build()


async def test_two_tenants_get_their_own_results(setup: Setup) -> None:
    bus, handlers, _cache = setup
    assert await bus.query_with_context(ListDocsQuery(), _ctx("acme")) == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await bus.query_with_context(ListDocsQuery(), _ctx("globex")) == ["globex: widgets"]
    # Each tenant's entry is reused by that tenant only.
    assert await bus.query_with_context(ListDocsQuery(), _ctx("acme")) == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await bus.query_with_context(ListDocsQuery(), _ctx("globex")) == ["globex: widgets"]
    assert type(handlers["docs"]).calls == 2


async def test_two_users_of_one_tenant_get_their_own_results(setup: Setup) -> None:
    bus, handlers, _cache = setup
    alice = await bus.query_with_context(ListDocsQuery(), _ctx("acme", "alice"))
    bob = await bus.query_with_context(ListDocsQuery(), _ctx("acme", "bob"))
    assert alice == bob == ["acme: Q3 payroll", "acme: bonus plan"]
    assert type(handlers["docs"]).calls == 2  # a context-aware result is keyed by user too


async def test_a_context_aware_handler_is_never_served_without_a_context(setup: Setup) -> None:
    bus, _handlers, _cache = setup
    assert await bus.query_with_context(ListDocsQuery(), _ctx("acme")) == ["acme: Q3 payroll", "acme: bonus plan"]
    with pytest.raises(QueryProcessingException, match="requires an ExecutionContext"):
        await bus.query(ListDocsQuery())


async def test_the_ambient_tenant_and_user_scope_a_plain_query(setup: Setup) -> None:
    bus, handlers, _cache = setup

    async def request(tenant: str, user: str) -> list[str]:
        # What the web filters set for one request: X-Tenant-Id and the authenticated principal.
        correlation.set_tenant_id(tenant)
        RequestContext.init().security_context = SecurityContext(user_id=user)
        return await bus.query(ListMyDocsQuery())

    async def as_user(tenant: str, user: str) -> list[str]:
        return await asyncio.create_task(request(tenant, user))  # a task of its own, like a request

    assert await as_user("acme", "alice") == ["acme: Q3 payroll"]
    assert await as_user("acme", "bob") == ["acme: bonus plan"]
    assert await as_user("globex", "carol") == ["globex: widgets"]
    assert await as_user("acme", "alice") == ["acme: Q3 payroll"]
    assert type(handlers["mine"]).calls == 3


async def test_an_explicitly_global_handler_is_shared(setup: Setup) -> None:
    bus, handlers, _cache = setup
    assert await bus.query_with_context(CountriesQuery(), _ctx("acme", "alice")) == ["ES", "US"]
    assert await bus.query_with_context(CountriesQuery(), _ctx("globex", "carol")) == ["ES", "US"]
    assert type(handlers["countries"]).calls == 1


async def test_clearing_a_key_clears_it_for_every_scope(setup: Setup) -> None:
    bus, handlers, cache = setup
    await bus.query_with_context(ListDocsQuery(), _ctx("acme"))
    await bus.query_with_context(ListDocsQuery(), _ctx("globex"))
    assert len(cache.get_keys()) == 2
    await bus.clear_cache(ListDocsQuery().get_cache_key())
    assert cache.get_keys() == []
    await bus.query_with_context(ListDocsQuery(), _ctx("acme"))
    assert type(handlers["docs"]).calls == 3
