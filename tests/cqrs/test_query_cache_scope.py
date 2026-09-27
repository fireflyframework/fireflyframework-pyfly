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
"""The query cache fails closed: it caches a call only when it can see who the caller is.

A ``USER`` entry needs the caller's user (the ``ExecutionContext``'s, or the authenticated principal of the
request), and a ``TENANT`` entry a tenant or organization from the ``ExecutionContext`` (or, failing that,
the user). The ``X-Tenant-Id`` header is client-supplied and never authenticated: its value is part of the
key, so it can only narrow an entry, but it never identifies a caller by itself. A call with no identity
the scope can use is not cached (one warning per handler), since caching it would share one entry among
every such caller: anonymous requests, message consumers, background jobs, and users the cache cannot see.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from pyfly.cache.adapters.memory import InMemoryCache
from pyfly.context.request_context import RequestContext
from pyfly.cqrs.cache.adapter import SCOPE_SEPARATOR, QueryCacheAdapter, scope_digest, scope_of
from pyfly.cqrs.cache.decorators import cacheable
from pyfly.cqrs.command.registry import HandlerRegistry
from pyfly.cqrs.context.execution_context import ExecutionContext, ExecutionContextBuilder
from pyfly.cqrs.decorators import query_handler
from pyfly.cqrs.query.bus import DefaultQueryBus
from pyfly.cqrs.query.handler import QueryHandler
from pyfly.cqrs.types import Query, QueryCacheScope
from pyfly.observability import correlation
from pyfly.security.context import SecurityContext


@dataclass(frozen=True)
class MyOrders(Query[list[str]]):
    pass


@dataclass(frozen=True)
class TenantPlans(Query[list[str]]):
    pass


@dataclass(frozen=True)
class Countries(Query[list[str]]):
    pass


@query_handler(cacheable=True)
class MyOrdersHandler(QueryHandler[MyOrders, list[str]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: MyOrders) -> list[str]:
        self.calls += 1
        return [f"order {self.calls}"]


@cacheable(scope=QueryCacheScope.TENANT)
@query_handler(cacheable=True)
class TenantPlansHandler(QueryHandler[TenantPlans, list[str]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: TenantPlans) -> list[str]:
        self.calls += 1
        return [f"plan {self.calls}"]


@cacheable(scope=QueryCacheScope.GLOBAL)
@query_handler(cacheable=True)
class CountriesHandler(QueryHandler[Countries, list[str]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: Countries) -> list[str]:
        self.calls += 1
        return ["ES", "US"]


@pytest.fixture
def handlers() -> tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]:
    return MyOrdersHandler(), TenantPlansHandler(), CountriesHandler()


@pytest.fixture
def bus(handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]) -> DefaultQueryBus:
    registry = HandlerRegistry()
    for handler in handlers:
        registry.register_query_handler(handler)
    return DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(InMemoryCache()))


def _ctx(*, tenant: str | None = None, organization: str | None = None, user: str | None = None) -> ExecutionContext:
    builder = ExecutionContextBuilder()
    if tenant is not None:
        builder = builder.with_tenant_id(tenant)
    if organization is not None:
        builder = builder.with_organization_id(organization)
    if user is not None:
        builder = builder.with_user_id(user)
    return builder.build()


async def _in_request(
    bus: DefaultQueryBus,
    query: Query[list[str]],
    *,
    principal: str | None = None,
    header: str | None = None,
    context: ExecutionContext | None = None,
) -> list[str]:
    """Run *query* in a task of its own, as a request with this principal and X-Tenant-Id header."""

    async def run() -> list[str]:
        correlation.set_tenant_id(header)
        RequestContext.init().security_context = (
            SecurityContext(user_id=principal) if principal is not None else SecurityContext.anonymous()
        )
        if context is None:
            return list(await bus.query(query))
        return list(await bus.query_with_context(query, context))

    return await asyncio.create_task(run())


# ── USER ────────────────────────────────────────────────────────


async def test_a_user_entry_is_not_cached_for_an_anonymous_caller(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    orders = handlers[0]
    await _in_request(bus, MyOrders())
    await _in_request(bus, MyOrders())
    assert orders.calls == 2


async def test_a_user_entry_needs_a_user_not_only_a_tenant(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    # The context names the tenant only: the handler may still depend on a user the cache cannot see.
    orders = handlers[0]
    await bus.query_with_context(MyOrders(), _ctx(tenant="acme"))
    await bus.query_with_context(MyOrders(), _ctx(tenant="acme"))
    assert orders.calls == 2


async def test_a_user_entry_is_reused_by_its_user_only(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    orders = handlers[0]
    assert await _in_request(bus, MyOrders(), principal="alice") == ["order 1"]
    assert await _in_request(bus, MyOrders(), principal="bob") == ["order 2"]
    assert await _in_request(bus, MyOrders(), principal="alice") == ["order 1"]
    assert await bus.query_with_context(MyOrders(), _ctx(user="alice")) == ["order 3"]  # the context's user
    assert await bus.query_with_context(MyOrders(), _ctx(user="alice")) == ["order 3"]
    assert orders.calls == 3


async def test_the_context_user_and_the_principal_both_narrow_an_entry(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    orders = handlers[0]
    on_behalf_of_alice = _ctx(user="alice")
    assert await _in_request(bus, MyOrders(), principal="bob", context=on_behalf_of_alice) == ["order 1"]
    assert await _in_request(bus, MyOrders(), principal="carol", context=on_behalf_of_alice) == ["order 2"]
    assert await _in_request(bus, MyOrders(), principal="bob", context=on_behalf_of_alice) == ["order 1"]
    assert orders.calls == 2


async def test_the_tenant_header_narrows_a_user_entry(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    orders = handlers[0]
    assert await _in_request(bus, MyOrders(), principal="alice", header="acme") == ["order 1"]
    assert await _in_request(bus, MyOrders(), principal="alice", header="globex") == ["order 2"]
    assert await _in_request(bus, MyOrders(), principal="alice", header="acme") == ["order 1"]
    assert orders.calls == 2


# ── TENANT ──────────────────────────────────────────────────────


async def test_a_tenant_entry_is_shared_by_the_users_of_the_context_tenant(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    plans = handlers[1]
    assert await bus.query_with_context(TenantPlans(), _ctx(tenant="acme", user="alice")) == ["plan 1"]
    assert await bus.query_with_context(TenantPlans(), _ctx(tenant="acme", user="bob")) == ["plan 1"]
    assert await bus.query_with_context(TenantPlans(), _ctx(tenant="acme")) == ["plan 1"]
    assert await bus.query_with_context(TenantPlans(), _ctx(organization="acme-eu")) == ["plan 2"]
    assert await bus.query_with_context(TenantPlans(), _ctx(tenant="globex", user="carol")) == ["plan 3"]
    assert plans.calls == 3


async def test_a_tenant_seen_only_in_the_header_does_not_identify_the_caller(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    plans = handlers[1]
    # Two users sending the same X-Tenant-Id: the header is not trusted, so each is keyed by their user.
    assert await _in_request(bus, TenantPlans(), principal="alice", header="acme") == ["plan 1"]
    assert await _in_request(bus, TenantPlans(), principal="mallory", header="acme") == ["plan 2"]
    assert await _in_request(bus, TenantPlans(), principal="alice", header="acme") == ["plan 1"]
    # An anonymous caller with only the header is not cached at all.
    assert await _in_request(bus, TenantPlans(), header="acme") == ["plan 3"]
    assert await _in_request(bus, TenantPlans(), header="acme") == ["plan 4"]
    assert plans.calls == 4


async def test_the_tenant_header_narrows_a_tenant_entry(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    plans = handlers[1]
    acme = _ctx(tenant="acme", user="alice")
    assert await _in_request(bus, TenantPlans(), context=acme, header="acme") == ["plan 1"]
    assert await _in_request(bus, TenantPlans(), context=acme, header="globex") == ["plan 2"]
    assert await _in_request(bus, TenantPlans(), context=acme, header="acme") == ["plan 1"]
    assert plans.calls == 2


# ── GLOBAL ──────────────────────────────────────────────────────


async def test_a_global_entry_is_shared_by_every_caller_anonymous_ones_included(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    countries = handlers[2]
    await _in_request(bus, Countries())
    await _in_request(bus, Countries(), principal="alice", header="acme")
    await bus.query_with_context(Countries(), _ctx(tenant="globex", user="carol"))
    assert countries.calls == 1


# ── the warning ─────────────────────────────────────────────────


async def test_an_unidentified_call_is_reported_once_per_handler(
    bus: DefaultQueryBus, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.query.bus")
    for _ in range(3):
        await _in_request(bus, MyOrders())
        await _in_request(bus, TenantPlans(), header="acme")
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "MyOrdersHandler" in messages[0] and "TenantPlansHandler" in messages[1]
    for message in messages:
        assert "no caller identity" in message
        assert "QueryCacheScope.GLOBAL" in message and "ExecutionContext" in message


# ── the functions ───────────────────────────────────────────────


def test_scope_of_is_none_when_the_scope_cannot_be_keyed() -> None:
    assert scope_of(QueryCacheScope.USER, None) is None
    assert scope_of(QueryCacheScope.USER, _ctx(tenant="acme", organization="eu")) is None
    assert scope_of(QueryCacheScope.TENANT, None) is None
    assert scope_of(QueryCacheScope.GLOBAL, None) == ()
    assert scope_of(QueryCacheScope.TENANT, _ctx(tenant="acme")) is not None
    assert scope_of(QueryCacheScope.USER, _ctx(user="alice")) is not None


def test_an_empty_identifier_is_no_identity() -> None:
    blank = ExecutionContextBuilder().with_user_id("").with_tenant_id("").with_organization_id("").build()
    assert scope_of(QueryCacheScope.USER, blank) is None
    assert scope_of(QueryCacheScope.TENANT, blank) is None


def test_scope_digest_tells_scopes_apart() -> None:
    alice = scope_of(QueryCacheScope.USER, _ctx(tenant="acme", user="alice"))
    bob = scope_of(QueryCacheScope.USER, _ctx(tenant="acme", user="bob"))
    acme = scope_of(QueryCacheScope.TENANT, _ctx(tenant="acme", user="alice"))
    assert alice is not None and bob is not None and acme is not None
    assert len({scope_digest(alice), scope_digest(bob), scope_digest(acme)}) == 3
    assert scope_digest(()) is None  # GLOBAL: the entry is not scoped


def test_scope_digest_is_a_full_sha256() -> None:
    # X-Tenant-Id is chosen by the client and is part of every scope: with a truncated digest a client could
    # search offline for a header value whose digest equals another caller's (a 64-bit digest falls to about
    # 2**64 / N hashes against N cached callers) and be served that caller's entry.
    alice = scope_of(QueryCacheScope.USER, _ctx(tenant="acme", user="alice"))
    assert alice is not None
    digest = scope_digest(alice)
    assert digest is not None
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    # The same caller gets the same entry key in every process, on every Python version: pinned.
    assert digest == "13d1be8317455ad02216564d6a726e8dd5271774d5e06d68942d59f59351c3a6"


def test_the_scope_encoding_is_unambiguous() -> None:
    # Each component is length-prefixed: values cannot run into each other, and None is not "None" or "".
    assert scope_digest((("tenant", "a|b"), ("user", "c"))) != scope_digest((("tenant", "a"), ("user", "b|c")))
    assert scope_digest((("tenant", "a"), ("user", "bc"))) != scope_digest((("tenant", "ab"), ("user", "c")))
    assert scope_digest((("tenant", None),)) != scope_digest((("tenant", "None"),))
    assert scope_digest((("tenant", None),)) != scope_digest((("tenant", ""),))
    assert scope_digest((("tenant", "x"),)) != scope_digest((("tenantx", None),))


async def test_an_identity_that_is_not_a_string_is_keyed_by_its_text(
    bus: DefaultQueryBus, handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler]
) -> None:
    # An application may build its ExecutionContext with a UUID: the cache never fails the query for it.
    orders = handlers[0]
    alice: Any = uuid.UUID(int=7)
    bob: Any = uuid.UUID(int=8)
    as_alice = ExecutionContextBuilder().with_tenant_id("acme").with_user_id(alice).build()
    assert await bus.query_with_context(MyOrders(), as_alice) == ["order 1"]
    assert await bus.query_with_context(MyOrders(), as_alice) == ["order 1"]
    assert await bus.query_with_context(MyOrders(), _ctx(tenant="acme", user=str(alice))) == ["order 1"]
    assert await bus.query_with_context(MyOrders(), _ctx(tenant="acme", user=str(bob))) == ["order 2"]
    assert orders.calls == 2


class _Opaque:
    """An identity with no text: the cache cannot key a call by it."""

    def __str__(self) -> str:
        raise RuntimeError("no text")


async def test_an_identity_the_cache_cannot_key_is_not_cached_and_never_fails_the_query(
    bus: DefaultQueryBus,
    handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler],
    caplog: pytest.LogCaptureFixture,
) -> None:
    orders = handlers[0]
    opaque: Any = _Opaque()
    context = ExecutionContextBuilder().with_tenant_id("acme").with_user_id(opaque).build()
    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.query.bus")
    assert await bus.query_with_context(MyOrders(), context) == ["order 1"]
    assert await bus.query_with_context(MyOrders(), context) == ["order 2"]  # not cached: it fails closed
    assert orders.calls == 2
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1 and "MyOrdersHandler" in messages[0] and "RuntimeError" in messages[0]


@dataclass(frozen=True)
class Unkeyable(Query[list[str]]):
    def get_cache_key(self) -> str | None:
        raise RecursionError("a field that contains itself")


@query_handler(cacheable=True)
class UnkeyableHandler(QueryHandler[Unkeyable, list[str]]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def do_handle(self, query: Unkeyable) -> list[str]:
        self.calls += 1
        return [f"answer {self.calls}"]


async def test_a_query_whose_key_cannot_be_computed_is_not_cached_and_never_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = UnkeyableHandler()
    registry = HandlerRegistry()
    registry.register_query_handler(handler)
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(InMemoryCache()))
    caplog.set_level(logging.WARNING, logger="pyfly.cqrs.query.bus")
    assert await bus.query_with_context(Unkeyable(), _ctx(user="alice")) == ["answer 1"]
    assert await bus.query_with_context(Unkeyable(), _ctx(user="alice")) == ["answer 2"]
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1 and "UnkeyableHandler" in messages[0] and "RecursionError" in messages[0]


async def test_callers_that_differ_only_in_the_tenant_header_or_the_principal_get_entries_of_their_own(
    handlers: tuple[MyOrdersHandler, TenantPlansHandler, CountriesHandler],
) -> None:
    cache = InMemoryCache()
    registry = HandlerRegistry()
    registry.register_query_handler(handlers[0])
    bus = DefaultQueryBus(registry=registry, cache_adapter=QueryCacheAdapter(cache))
    assert await _in_request(bus, MyOrders(), principal="alice", header="acme") == ["order 1"]
    assert await _in_request(bus, MyOrders(), principal="alice", header="x262424") == ["order 2"]  # a forged header
    assert await _in_request(bus, MyOrders(), principal="mallory", header="acme") == ["order 3"]
    digests = [key.rsplit(SCOPE_SEPARATOR, 1)[1] for key in cache.get_keys() if SCOPE_SEPARATOR in key]
    assert len(set(digests)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests)
