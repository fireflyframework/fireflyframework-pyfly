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
"""FlagManagement: the management API's operations and error codes (spec 4.8), shared by actuator, admin and CLI."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.transaction_context import get_transaction_context, set_transaction_context

from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.management import (
    ERROR_STATUS,
    FlagManagement,
    FlagManagementError,
    actor_from_security,
    iso_instant,
)
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.feature_flags.sources.store import StoreFlagSource
from pyfly.feature_flags.store.memory import MemoryFlagStore
from pyfly.feature_flags.store.writer import FlagStoreWriter
from pyfly.security.context import SecurityContext
from pyfly.security.context_holder import SecurityContextHolder
from tests.feature_flags.support import StaticSource, bool_flag

MANAGEMENT_CASES = json.loads((Path(__file__).parent / "conformance" / "management-vectors.json").read_text())["cases"]

CONFIG: dict[str, Any] = {
    "kill": True,
    "theme": {
        "state": "ENABLED",
        "variants": {"light": "light", "dark": "dark"},
        "defaultVariant": "light",
        "targeting": {"if": [{"==": [{"var": "plan"}, "pro"]}, "dark", None]},
        "metadata": {"owner": "web", "kind": "experiment", "expires": "2020-01-01"},
    },
}


async def _management(*, writes: bool = True, store: bool = True) -> AsyncIterator[FlagManagement]:
    flag_store = MemoryFlagStore() if store else None
    sources: list[Any] = [StaticSource("config", CONFIG)]
    if flag_store is not None:
        sources.append(StoreFlagSource(flag_store))
    provider = FireflyFlagProvider()
    registry = FlagRegistry(sources, provider)
    await registry.start()
    facade = FeatureFlags(
        OpenFeatureClient(domain="mgmt", version=None), EvaluationContextResolver(), registry=registry
    )
    binding = OpenFeatureBinding(provider, facade, domain="mgmt")
    await binding.start()
    writer = FlagStoreWriter(flag_store, registry) if flag_store is not None else None
    yield FlagManagement(facade, registry=registry, store=flag_store, writer=writer, writes_enabled=writes)
    await binding.stop()
    await registry.stop()


@pytest.fixture
async def management() -> AsyncIterator[FlagManagement]:
    async for value in _management():
        yield value


def test_error_codes_carry_the_contract_statuses() -> None:
    assert ERROR_STATUS == {
        "writes-disabled": 403,
        "not-writable": 409,
        "invalid-definition": 422,
        "unknown-flag": 404,
        "unknown-variant": 422,
        "conflict": 409,
        "bad-request": 400,
    }
    error = FlagManagementError("conflict", "expected 2")
    assert error.status == 409 and error.to_body() == {"error": "conflict", "message": "expected 2"}


async def test_the_overview_lists_provider_sources_and_flags(management: FlagManagement) -> None:
    overview = await management.overview()
    assert overview["provider"] == {"name": "firefly", "status": "READY"}
    assert (overview["writable"], overview["writesEnabled"]) == (True, True)
    assert [(s["name"], s["enabled"], s["status"], s["flags"]) for s in overview["sources"]] == [
        ("config", True, "UP", 2),
        ("store", True, "UP", 0),
    ]
    assert overview["sources"][1]["revision"] == "0" and overview["sources"][0]["error"] is None
    assert overview["sources"][0]["lastRefresh"].endswith("Z")
    theme = next(flag for flag in overview["flags"] if flag["key"] == "theme")
    assert theme == {
        "key": "theme",
        "state": "ENABLED",
        "type": "string",
        "variants": ["light", "dark"],
        "defaultVariant": "light",
        "targeting": True,
        "origin": "config",
        "overrides": [],
        "metadata": {"owner": "web", "kind": "experiment", "expires": "2020-01-01"},
        "expired": True,
        "version": None,
    }
    assert [flag["key"] for flag in overview["flags"]] == ["kill", "theme"]


async def test_evaluate_previews_with_the_explicit_context_only(management: FlagManagement) -> None:
    with SecurityContextHolder.using(SecurityContext(user_id="caller", attributes={"plan": "pro"})):
        result = await management.execute("theme", {"action": "evaluate", "context": {"plan": "pro"}}, actor="x")
        plain = await management.evaluate("theme")
    assert result == {
        "key": "theme",
        "value": "dark",
        "variant": "dark",
        "reason": "TARGETING_MATCH",
        "errorCode": None,
        "metadata": {"owner": "web", "kind": "experiment", "expires": "2020-01-01"},
    }
    assert plain["variant"] == "light" and plain["reason"] == "DEFAULT"  # the caller's principal is not used
    with pytest.raises(FlagManagementError) as raised:
        await management.evaluate("missing")
    assert raised.value.code == "unknown-flag"
    with pytest.raises(FlagManagementError) as bad:
        await management.evaluate("theme", context=["not", "an", "object"])
    assert bad.value.code == "bad-request"


async def test_evaluate_does_not_inherit_sdk_transaction_context(management: FlagManagement) -> None:
    previous = get_transaction_context()
    set_transaction_context(EvaluationContext("tx-user", {"plan": "pro"}))
    try:
        result = await management.evaluate("theme")
        assert result["variant"] == "light"
        assert get_transaction_context().targeting_key == "tx-user"
    finally:
        set_transaction_context(previous)


async def test_enable_and_disable_copy_the_effective_definition_into_the_store(management: FlagManagement) -> None:
    detail = await management.execute("kill", {"action": "disable"}, actor="ops")
    assert detail["origin"] == "store" and detail["definition"]["state"] == "DISABLED" and detail["version"] == 1
    assert [layer["source"] for layer in detail["layers"]] == ["config", "store"]
    assert [(h["action"], h["actor"]) for h in detail["history"]] == [("put", "ops")]
    assert detail["history"][0]["changedAt"].endswith("Z")
    assert management.facade.is_enabled("kill", default=True) is True  # DISABLED returns the caller default
    again = await management.execute("kill", {"action": "enable", "expectedVersion": 1}, actor="ops")
    assert again["definition"]["state"] == "ENABLED" and again["version"] == 2


async def test_default_variant_checks_the_variant(management: FlagManagement) -> None:
    changed = await management.execute("theme", {"action": "default-variant", "variant": "dark"}, actor="ops")
    assert changed["definition"]["defaultVariant"] == "dark"
    for body, code in [
        ({"action": "default-variant", "variant": "neon"}, "unknown-variant"),
        ({"action": "default-variant"}, "bad-request"),
        ({"action": "default-variant", "variant": 3}, "bad-request"),
    ]:
        with pytest.raises(FlagManagementError) as raised:
            await management.execute("theme", body, actor="ops")
        assert raised.value.code == code


async def test_put_validates_and_delete_reverts_to_the_next_layer(management: FlagManagement) -> None:
    with pytest.raises(FlagManagementError) as invalid:
        await management.execute("kill", {"action": "put", "definition": {"state": "ON"}}, actor="ops")
    assert invalid.value.code == "invalid-definition" and "state must be ENABLED or DISABLED" in invalid.value.message
    with pytest.raises(FlagManagementError) as not_object:
        await management.execute("kill", {"action": "put", "definition": True}, actor="ops")
    assert (
        not_object.value.code == "invalid-definition"
        and "flag definition must be an object" in not_object.value.message
    )
    stored = await management.execute("kill", {"action": "put", "definition": bool_flag("off")}, actor="ops")
    assert stored["origin"] == "store" and stored["definition"] == bool_flag("off")
    reverted = await management.execute("kill", {"action": "delete"}, actor="ops")
    assert reverted["origin"] == "config" and reverted["version"] is None
    with pytest.raises(FlagManagementError) as nothing_stored:
        await management.execute("kill", {"action": "delete"}, actor="ops")
    assert nothing_stored.value.code == "unknown-flag"
    await management.execute("brand-new", {"action": "put", "definition": bool_flag()}, actor="ops")
    assert await management.execute("brand-new", {"action": "delete"}, actor="ops") == {
        "key": "brand-new",
        "deleted": True,
    }


async def test_expected_version_is_checked(management: FlagManagement) -> None:
    with pytest.raises(FlagManagementError) as conflict:
        await management.execute("kill", {"action": "disable", "expectedVersion": 3}, actor="ops")
    assert conflict.value.code == "conflict"
    for bad in (-1, "1", True, 1.5):
        with pytest.raises(FlagManagementError) as raised:
            await management.execute("kill", {"action": "disable", "expectedVersion": bad}, actor="ops")
        assert raised.value.code == "bad-request"


@pytest.mark.parametrize("body", [{}, {"action": ""}, {"action": "explode"}, {"action": 7}])
async def test_a_malformed_body_is_a_bad_request(management: FlagManagement, body: dict[str, Any]) -> None:
    with pytest.raises(FlagManagementError) as raised:
        await management.execute("kill", body, actor="ops")
    assert raised.value.code == "bad-request"


async def test_unknown_flags(management: FlagManagement) -> None:
    assert await management.detail("missing") is None
    with pytest.raises(FlagManagementError) as raised:
        await management.execute("missing", {"action": "enable"}, actor="ops")
    assert raised.value.code == "unknown-flag"


async def test_writes_need_the_switch_and_a_store() -> None:
    async for disabled in _management(writes=False):
        with pytest.raises(FlagManagementError) as raised:
            await disabled.execute("kill", {"action": "disable"}, actor="ops")
        assert raised.value.code == "writes-disabled"
        assert (await disabled.execute("kill", {"action": "evaluate"}, actor="ops"))["value"] is True
    async for storeless in _management(store=False):
        with pytest.raises(FlagManagementError) as raised:
            await storeless.execute("kill", {"action": "disable"}, actor="ops")
        assert raised.value.code == "not-writable"
        assert (await storeless.overview())["writable"] is False


async def test_history_is_newest_first_and_capped_at_fifty(management: FlagManagement) -> None:
    for index in range(55):
        await management.execute("kill", {"action": "disable" if index % 2 else "enable"}, actor=f"op-{index}")
    detail = await management.detail("kill")
    assert detail is not None and len(detail["history"]) == 50 and detail["history"][0]["actor"] == "op-54"


async def test_an_external_provider_has_no_flag_list() -> None:
    from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider

    facade = FeatureFlags(OpenFeatureClient(domain="external", version=None), EvaluationContextResolver())
    binding = OpenFeatureBinding(InMemoryProvider({"x": InMemoryFlag("on", {"on": True})}), facade, domain="external")
    await binding.start()
    management = FlagManagement(facade)
    overview = await management.overview()
    assert overview["provider"]["name"] == "In-Memory Provider"
    assert (overview["writable"], overview["sources"], overview["flags"]) == (False, [], [])
    assert await management.detail("x") is None
    assert (await management.evaluate("x"))["value"] is True
    await binding.stop()


def test_the_actor_is_the_principal_or_the_fallback() -> None:
    assert actor_from_security("actuator") == "actuator"
    with SecurityContextHolder.using(SecurityContext(user_id="ana")):
        assert actor_from_security("actuator") == "ana"


def test_instants_are_utc_with_a_z() -> None:
    assert iso_instant(dt.datetime(2026, 10, 1, 9, 30, 5, 123, tzinfo=dt.UTC)) == "2026-10-01T09:30:05Z"
    assert (
        iso_instant(dt.datetime(2026, 10, 1, 11, 30, tzinfo=dt.timezone(dt.timedelta(hours=2))))
        == "2026-10-01T09:30:00Z"
    )
    assert iso_instant(None) is None


@pytest.mark.parametrize("case", MANAGEMENT_CASES, ids=lambda case: case["name"])
async def test_shared_management_receipts(monkeypatch: pytest.MonkeyPatch, case: dict[str, Any]) -> None:
    flag_store = MemoryFlagStore()
    if case["initialDefinition"] is not None:
        await flag_store.put("checkout", case["initialDefinition"], actor="seed")
    provider = FireflyFlagProvider()
    source = StoreFlagSource(flag_store)
    registry = FlagRegistry([source], provider)
    await registry.start()
    facade = FeatureFlags(
        OpenFeatureClient(domain="receipt", version=None), EvaluationContextResolver(), registry=registry
    )
    management = FlagManagement(
        facade,
        registry=registry,
        store=flag_store,
        writer=FlagStoreWriter(flag_store, registry),
        writes_enabled=True,
    )
    if case["refresh"] == "fail":

        async def fail_load() -> None:
            raise RuntimeError("controlled refresh failure")

        monkeypatch.setattr(source, "load", fail_load)
    elif case["refresh"] == "refuse":

        def refuse_update(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("controlled provider refusal")

        monkeypatch.setattr(provider, "update", refuse_update)
    body = {"action": case["action"]}
    if case["action"] == "put":
        body["definition"] = case["definition"]
    result = await management.execute("checkout", body, actor="ops")
    assert (await flag_store.get("checkout") is None) == (case["action"] == "delete")
    expectation = case["expect"]
    if "body" in expectation:
        assert result == expectation["body"]
    else:
        assert result["definition"] == expectation["detailDefinition"]
        assert (await management.detail("checkout"))["definition"] == expectation["detailDefinition"]
    await registry.stop()


async def test_diagnostic_logger_failure_does_not_escape_a_committed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pyfly.feature_flags.management as management_module

    flag_store = MemoryFlagStore()
    source = StoreFlagSource(flag_store)
    registry = FlagRegistry([source], FireflyFlagProvider())
    await registry.start()
    facade = FeatureFlags(
        OpenFeatureClient(domain="diagnostic", version=None), EvaluationContextResolver(), registry=registry
    )
    management = FlagManagement(
        facade,
        registry=registry,
        store=flag_store,
        writer=FlagStoreWriter(flag_store, registry),
        writes_enabled=True,
    )

    async def failed_history(key: str, limit: int = 50) -> None:
        raise RuntimeError("history is down")

    def failed_warning(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("logger is down")

    monkeypatch.setattr(flag_store, "history", failed_history)
    monkeypatch.setattr(management_module._logger, "warning", failed_warning)
    result = await management.put("checkout", bool_flag(), actor="ops")
    assert result["definition"] == bool_flag()
    assert (await flag_store.get("checkout")).version == 1
    await registry.stop()
