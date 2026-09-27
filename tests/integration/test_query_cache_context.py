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
user), or the authenticated user of a plain ``query`` (and the ``X-Tenant-Id`` header, which narrows an
entry but never identifies a caller: a client sets it). The cache fails closed: a call whose caller it
cannot identify is not cached at all, unless the handler declares its data the same for everyone.

The handler reads real rows scoped by tenant from a SQLite file database (foreign keys on) and from
PostgreSQL, as a multi-tenant service does.
"""

from __future__ import annotations

import asyncio
import contextvars
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


@dataclass(frozen=True)
class TenantDocsQuery(Query[list[str]]):
    status: str = "open"


@dataclass(frozen=True)
class SharedDocsQuery(Query[list[str]]):
    status: str = "open"


@dataclass(frozen=True)
class PriceListQuery(Query[list[str]]):
    status: str = "open"


current_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tenant", default=None)
"""Where an application keeps the tenant it resolved (the tenant-GUC pattern of
``pyfly.data.relational.dialect_customizers``): invisible to the query cache."""


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

    @cacheable(scope=QueryCacheScope.TENANT)
    @query_handler(cacheable=True)
    class TenantDocsHandler(QueryHandler[TenantDocsQuery, list[str]]):
        """Takes the tenant from the principal (a JWT claim), not from X-Tenant-Id: the cache sees no tenant."""

        calls = 0

        async def do_handle(self, query: TenantDocsQuery) -> list[str]:
            type(self).calls += 1
            request = RequestContext.current()
            principal = request.security_context if request is not None else None
            assert principal is not None
            return await _docs(engine, principal.attributes["tenant"])

    @cacheable(scope=QueryCacheScope.TENANT)
    @query_handler(cacheable=True)
    class SharedDocsHandler(ContextAwareQueryHandler[SharedDocsQuery, list[str]]):
        """Every user of a tenant sees the same documents: the tenant comes from the ExecutionContext."""

        calls = 0

        async def do_handle_with_context(self, query: SharedDocsQuery, context: ExecutionContext) -> list[str]:
            type(self).calls += 1
            return await _docs(engine, context.tenant_id)

    @query_handler(cacheable=True)
    class PriceListHandler(QueryHandler[PriceListQuery, list[str]]):
        """The default USER scope; the tenant lives in an application ContextVar the cache cannot see."""

        calls = 0

        async def do_handle(self, query: PriceListQuery) -> list[str]:
            type(self).calls += 1
            return await _docs(engine, current_tenant.get())

    return {
        "docs": ListDocsHandler(),
        "mine": ListMyDocsHandler(),
        "countries": CountriesHandler(),
        "tenant_docs": TenantDocsHandler(),
        "shared_docs": SharedDocsHandler(),
        "prices": PriceListHandler(),
    }


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
    acme, globex = _ctx("acme", "alice"), _ctx("globex", "carol")
    assert await bus.query_with_context(ListDocsQuery(), acme) == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await bus.query_with_context(ListDocsQuery(), globex) == ["globex: widgets"]
    # Each tenant's entry is reused by that tenant only.
    assert await bus.query_with_context(ListDocsQuery(), acme) == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await bus.query_with_context(ListDocsQuery(), globex) == ["globex: widgets"]
    assert type(handlers["docs"]).calls == 2


async def test_a_tenant_scoped_entry_is_shared_by_the_users_of_the_context_tenant(setup: Setup) -> None:
    bus, handlers, _cache = setup
    assert await bus.query_with_context(SharedDocsQuery(), _ctx("acme", "alice")) == [
        "acme: Q3 payroll",
        "acme: bonus plan",
    ]
    assert await bus.query_with_context(SharedDocsQuery(), _ctx("acme", "bob")) == [
        "acme: Q3 payroll",
        "acme: bonus plan",
    ]
    assert await bus.query_with_context(SharedDocsQuery(), _ctx("globex", "carol")) == ["globex: widgets"]
    assert type(handlers["shared_docs"]).calls == 2


async def test_two_users_of_one_tenant_get_their_own_results(setup: Setup) -> None:
    bus, handlers, _cache = setup
    alice = await bus.query_with_context(ListDocsQuery(), _ctx("acme", "alice"))
    bob = await bus.query_with_context(ListDocsQuery(), _ctx("acme", "bob"))
    assert alice == bob == ["acme: Q3 payroll", "acme: bonus plan"]
    assert type(handlers["docs"]).calls == 2  # a context-aware result is keyed by user too


async def test_a_context_aware_handler_is_never_served_without_a_context(setup: Setup) -> None:
    bus, _handlers, _cache = setup
    assert await bus.query_with_context(ListDocsQuery(), _ctx("acme", "alice")) == [
        "acme: Q3 payroll",
        "acme: bonus plan",
    ]
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


async def test_a_tenant_scoped_handler_keys_by_user_when_no_tenant_is_visible(setup: Setup) -> None:
    bus, handlers, _cache = setup

    async def request(user: str, tenant_claim: str) -> list[str]:
        # The tenant is a claim of the principal: no X-Tenant-Id, no ExecutionContext.
        RequestContext.init().security_context = SecurityContext(user_id=user, attributes={"tenant": tenant_claim})
        return await bus.query(TenantDocsQuery())

    async def as_user(user: str, tenant_claim: str) -> list[str]:
        return await asyncio.create_task(request(user, tenant_claim))

    assert await as_user("alice", "acme") == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await as_user("carol", "globex") == ["globex: widgets"]
    assert await as_user("alice", "acme") == ["acme: Q3 payroll", "acme: bonus plan"]
    assert type(handlers["tenant_docs"]).calls == 2


async def test_a_caller_the_cache_cannot_identify_is_never_served_another_callers_entry(setup: Setup) -> None:
    bus, handlers, _cache = setup

    async def request(tenant: str) -> list[str]:
        # The application resolved the tenant (from the host name, a session) into its own ContextVar, and
        # the caller is anonymous to the cache: no ExecutionContext, no authenticated principal.
        current_tenant.set(tenant)
        RequestContext.init().security_context = SecurityContext.anonymous()
        return await bus.query(PriceListQuery())

    async def as_tenant(tenant: str) -> list[str]:
        return await asyncio.create_task(request(tenant))

    assert await as_tenant("acme") == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await as_tenant("globex") == ["globex: widgets"]
    assert await as_tenant("acme") == ["acme: Q3 payroll", "acme: bonus plan"]
    assert type(handlers["prices"]).calls == 3  # not cached: nobody is served another caller's entry


async def test_a_forged_tenant_header_can_neither_read_nor_poison_another_tenants_entry(setup: Setup) -> None:
    bus, handlers, cache = setup

    async def request(user: str, tenant_claim: str, header: str) -> list[str]:
        # The application trusts the principal's claim; the client also sends X-Tenant-Id, which the
        # correlation filter copies into the ambient tenant without authenticating it.
        correlation.set_tenant_id(header)
        RequestContext.init().security_context = SecurityContext(user_id=user, attributes={"tenant": tenant_claim})
        return await bus.query(TenantDocsQuery())

    async def as_user(user: str, tenant_claim: str, header: str) -> list[str]:
        return await asyncio.create_task(request(user, tenant_claim, header))

    # carol (globex) fills an entry; mallory (acme) sends X-Tenant-Id: globex and cannot read it.
    assert await as_user("carol", "globex", "globex") == ["globex: widgets"]
    assert await as_user("mallory", "acme", "globex") == ["acme: Q3 payroll", "acme: bonus plan"]

    # On an empty cache, mallory goes first with the forged header: carol is not served what mallory got.
    await cache.clear()
    assert await as_user("mallory", "acme", "globex") == ["acme: Q3 payroll", "acme: bonus plan"]
    assert await as_user("carol", "globex", "globex") == ["globex: widgets"]
    assert type(handlers["tenant_docs"]).calls == 4


async def test_an_explicitly_global_handler_is_shared(setup: Setup) -> None:
    bus, handlers, _cache = setup
    assert await bus.query_with_context(CountriesQuery(), _ctx("acme", "alice")) == ["ES", "US"]
    assert await bus.query_with_context(CountriesQuery(), _ctx("globex", "carol")) == ["ES", "US"]
    assert type(handlers["countries"]).calls == 1


async def test_clearing_a_key_clears_it_for_every_scope(setup: Setup) -> None:
    bus, handlers, _cache = setup
    acme, globex = _ctx("acme", "alice"), _ctx("globex", "carol")
    await bus.query_with_context(ListDocsQuery(), acme)
    await bus.query_with_context(ListDocsQuery(), globex)
    await bus.query_with_context(ListDocsQuery(), acme)
    assert type(handlers["docs"]).calls == 2
    await bus.clear_cache(ListDocsQuery().get_cache_key())
    await bus.query_with_context(ListDocsQuery(), acme)
    await bus.query_with_context(ListDocsQuery(), globex)
    assert type(handlers["docs"]).calls == 4


async def test_evicting_a_key_never_scans_the_cache(setup: Setup) -> None:
    bus, _handlers, cache = setup
    scans: list[str] = []
    evict_by_prefix = cache.evict_by_prefix

    async def recorded(prefix: str) -> int:
        scans.append(prefix)
        return await evict_by_prefix(prefix)

    cache.evict_by_prefix = recorded  # type: ignore[method-assign]
    for tenant in ("acme", "globex", "initech"):
        await bus.query_with_context(ListDocsQuery(), _ctx(tenant, "admin"))
    await bus.clear_cache(ListDocsQuery().get_cache_key())
    assert scans == []  # one write moves the key to a new generation, whatever the number of scopes
