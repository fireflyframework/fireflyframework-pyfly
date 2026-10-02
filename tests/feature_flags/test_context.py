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
"""The ambient evaluation context (spec 4.4) and the per-request OpenFeature transaction context (spec 6.2)."""

from __future__ import annotations

import logging
import typing
from typing import Any

import httpx
import pytest
from openfeature import api
from openfeature.evaluation_context import EvaluationContext
from openfeature.transaction_context import ContextVarsTransactionContextPropagator
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from pyfly.container.container import Container
from pyfly.container.ordering import HIGHEST_PRECEDENCE, get_order, order
from pyfly.context.events import ContextRefreshedEvent
from pyfly.feature_flags.context import (
    ApplicationContextContributor,
    EvaluationContextContributor,
    EvaluationContextResolver,
    FeatureFlagsContextFilter,
    SecurityContextContributor,
    TenantContextContributor,
)
from pyfly.observability.correlation import set_tenant_id
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
from pyfly.web.ports.filter import CallNext

ALICE = SecurityContext(user_id="alice", roles=["ROLE_ADMIN", "beta"], attributes={"tenant": "acme", "org": "o-7"})


def _resolver(**tenant: Any) -> EvaluationContextResolver:
    return EvaluationContextResolver(
        [
            SecurityContextContributor(),
            TenantContextContributor(**tenant),
            ApplicationContextContributor(application="shop", profiles=["prod", "eu"]),
        ]
    )


def test_an_authenticated_principal_gives_targeting_key_roles_and_tenant() -> None:
    with SecurityContextHolder.using(ALICE):
        context = _resolver().resolve()
    assert context.targeting_key == "alice"
    assert context.attributes == {
        "roles": ["ADMIN", "beta"],
        "tenant": "acme",
        "application": "shop",
        "profiles": ["prod", "eu"],
    }


def test_anonymous_traffic_has_no_targeting_key() -> None:
    context = _resolver().resolve()
    assert context.targeting_key is None
    assert context.attributes == {"application": "shop", "profiles": ["prod", "eu"]}


def test_the_tenant_attribute_is_configurable() -> None:
    with SecurityContextHolder.using(ALICE):
        assert _resolver(tenant_attribute="org").resolve().attributes["tenant"] == "o-7"


def test_the_tenant_header_is_used_only_when_trusted() -> None:
    set_tenant_id("from-header")
    try:
        assert "tenant" not in _resolver().resolve().attributes
        assert _resolver(trust_tenant_header=True).resolve().attributes["tenant"] == "from-header"
        with SecurityContextHolder.using(ALICE):  # the principal's attribute wins over the header
            assert _resolver(trust_tenant_header=True).resolve().attributes["tenant"] == "acme"
    finally:
        set_tenant_id(None)


class PlanContributor:
    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes["plan"] = "pro"
        attributes["application"] = "shop-eu"  # contributors run after the built-ins and may override them


@order(10)
class LateContributor:
    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes["plan"] = attributes.get("plan", "") + "+late"


class BrokenContributor:
    def contribute(self, attributes: dict[str, Any]) -> None:
        raise RuntimeError("contributor bug")


def test_explicit_contributors_run_after_the_builtins_and_a_broken_one_is_skipped() -> None:
    resolver = EvaluationContextResolver(
        [ApplicationContextContributor(application="shop", profiles=[])],
        contributors=[BrokenContributor(), PlanContributor()],
    )
    assert resolver.attributes() == {"application": "shop-eu", "profiles": [], "plan": "pro"}


async def test_contributor_beans_are_found_in_the_container_in_order() -> None:
    container = Container()
    container.register_instance(LateContributor, LateContributor())
    container.register_instance(PlanContributor, PlanContributor())
    resolver = EvaluationContextResolver([], container=container)
    await resolver.on_context_refreshed(ContextRefreshedEvent())
    assert resolver.attributes()["plan"] == "pro+late"  # PlanContributor (order 0) before LateContributor (10)


def test_a_failing_contributor_warns_once_per_class_and_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = EvaluationContextResolver(contributors=[BrokenContributor(), PlanContributor()])
    with caplog.at_level(logging.DEBUG, logger="pyfly.feature_flags.context"):
        assert resolver.attributes()["plan"] == "pro"
        first = [r for r in caplog.records if r.getMessage() == "feature_flag_context_contributor_failed"]
        assert [(r.levelno, getattr(r, "contributor", None)) for r in first] == [(logging.WARNING, "BrokenContributor")]
        assert first[0].exc_info is not None  # the traceback is kept

        caplog.clear()
        assert resolver.attributes()["plan"] == "pro"
    repeats = [r for r in caplog.records if r.getMessage() == "feature_flag_context_contributor_failed"]
    assert [(r.levelno, getattr(r, "contributor", None)) for r in repeats] == [(logging.DEBUG, "BrokenContributor")]


def test_each_failing_contributor_class_gets_its_own_warning(caplog: pytest.LogCaptureFixture) -> None:
    class AnotherBrokenContributor:
        def contribute(self, attributes: dict[str, Any]) -> None:
            raise RuntimeError("another bug")

    resolver = EvaluationContextResolver(contributors=[BrokenContributor(), AnotherBrokenContributor()])
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.context"):
        resolver.attributes()
        resolver.attributes()
    warned = [getattr(r, "contributor", None) for r in caplog.records if r.levelno == logging.WARNING]
    assert warned == ["BrokenContributor", "AnotherBrokenContributor"]


def test_before_the_refresh_the_container_is_scanned_on_each_call() -> None:
    container = Container()
    resolver = EvaluationContextResolver([], container=container)
    assert resolver.attributes() == {}
    container.register_instance(PlanContributor, PlanContributor())
    assert resolver.attributes() == {"plan": "pro", "application": "shop-eu"}


def test_a_builtin_that_is_also_a_bean_contributes_once() -> None:
    class Counting:
        calls = 0

        def contribute(self, attributes: dict[str, Any]) -> None:
            type(self).calls += 1

    builtin = Counting()
    container = Container()
    container.register_instance(Counting, builtin)
    EvaluationContextResolver([builtin], container=container).attributes()
    assert Counting.calls == 1


def test_the_refresh_listener_declares_its_event_type_for_the_application_context() -> None:
    hints = typing.get_type_hints(EvaluationContextResolver([]).on_context_refreshed)
    assert hints["event"] is ContextRefreshedEvent


def test_a_class_with_a_contribute_method_satisfies_the_contributor_port() -> None:
    assert isinstance(PlanContributor(), EvaluationContextContributor)
    assert not isinstance(object(), EvaluationContextContributor)


class AnonymousIdContributor:
    """The documented way to give anonymous traffic a stable id for percentage rollouts."""

    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes.setdefault("targetingKey", "anon-42")


def test_a_contributor_can_supply_the_targeting_key_of_anonymous_traffic() -> None:
    resolver = EvaluationContextResolver([SecurityContextContributor()], contributors=[AnonymousIdContributor()])
    anonymous = resolver.resolve()
    assert anonymous.targeting_key == "anon-42"
    assert "targetingKey" not in anonymous.attributes
    with SecurityContextHolder.using(ALICE):  # setdefault: the principal's own key is kept
        assert resolver.resolve().targeting_key == "alice"


def test_a_principal_with_an_empty_tenant_attribute_does_not_fall_back_to_the_header() -> None:
    blank = SecurityContext(user_id="carol", attributes={"tenant": ""})
    set_tenant_id("from-header")
    try:
        with SecurityContextHolder.using(blank):
            assert "tenant" not in _resolver(trust_tenant_header=True).resolve().attributes
        with SecurityContextHolder.using(SecurityContext(user_id="dave")):  # no attribute at all: the header counts
            assert _resolver(trust_tenant_header=True).resolve().attributes["tenant"] == "from-header"
    finally:
        set_tenant_id(None)


def test_the_filter_runs_after_every_security_filter() -> None:
    assert get_order(FeatureFlagsContextFilter) == HIGHEST_PRECEDENCE + 400


def test_process_attributes_hold_only_the_application_and_profiles() -> None:
    with SecurityContextHolder.using(ALICE):
        assert _resolver().process_attributes() == {"application": "shop", "profiles": ["prod", "eu"]}


@order(HIGHEST_PRECEDENCE + 220)
class FakeAuthentication:
    """Stands in for the security filters: authenticates the X-User header."""

    def should_not_filter(self, request: Any) -> bool:
        return False

    async def do_filter(self, request: Any, call_next: CallNext) -> Any:
        user = request.headers.get("x-user")
        if user is None:
            return await call_next(request)
        token = SecurityContextHolder.set_context(SecurityContext(user_id=user, roles=["ROLE_beta"]))
        try:
            return await call_next(request)
        finally:
            SecurityContextHolder.reset_context(token)


async def test_the_filter_sets_the_transaction_context_for_third_party_clients_and_restores_it() -> None:
    api.set_transaction_context_propagator(ContextVarsTransactionContextPropagator())
    sentinel = EvaluationContext(targeting_key="outside-the-request", attributes={"marker": "sentinel"})
    api.set_transaction_context(sentinel)

    async def echo(request: Request) -> JSONResponse:
        context = api.get_transaction_context()
        return JSONResponse({"targetingKey": context.targeting_key, "roles": context.attributes.get("roles")})

    filters = [FakeAuthentication(), FeatureFlagsContextFilter(_resolver())]
    app = Starlette(routes=[Route("/", echo)], middleware=[Middleware(WebFilterChainMiddleware, filters=filters)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        anonymous = (await client.get("/")).json()
        assert api.get_transaction_context() is sentinel  # restored after the anonymous request
        signed_in = (await client.get("/", headers={"x-user": "bob"})).json()
    assert anonymous == {"targetingKey": None, "roles": None}
    assert signed_in == {"targetingKey": "bob", "roles": ["beta"]}
    # the last request was the signed-in one: only the restore puts back what was there before it
    assert api.get_transaction_context() is sentinel
