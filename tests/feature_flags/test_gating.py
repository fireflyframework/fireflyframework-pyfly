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
"""@feature_flag on functions, methods and classes, sync and async, routes included (spec 6.2, D8)."""

from __future__ import annotations

import inspect
import subprocess
import sys
from collections.abc import AsyncIterator
from functools import partialmethod
from typing import Any

import httpx
import pytest
from openfeature.client import OpenFeatureClient

from pyfly.container import rest_controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.gating import (
    FeatureFlagDisabledException,
    FeatureFlagForbiddenException,
    FeatureFlagNotFoundException,
    FeatureFlagUnavailableException,
    feature_flag,
    feature_flag_disabled,
)
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FlagRegistry
from pyfly.kernel.exceptions import ForbiddenException, ResourceNotFoundException, ServiceUnavailableException
from pyfly.web import PathVar, get_mapping, request_mapping
from pyfly.web.adapters.starlette.app import create_app
from tests.feature_flags.support import StaticSource, bool_flag

FLAGS: dict[str, Any] = {
    "on": True,
    "off": False,
    "paused": {**bool_flag("on"), "state": "DISABLED"},
    "no-default": {"state": "ENABLED", "variants": {"on": True, "off": False}},
    "checkout": {"state": "ENABLED", "variants": {"control": "v1", "treatment": "v2"}, "defaultVariant": "treatment"},
}


@pytest.fixture
async def flags() -> AsyncIterator[FeatureFlags]:
    provider = FireflyFlagProvider()
    registry = FlagRegistry([StaticSource("config", FLAGS)], provider)
    await registry.start()
    facade = FeatureFlags(
        OpenFeatureClient(domain="gating", version=None), EvaluationContextResolver(), registry=registry
    )
    binding = OpenFeatureBinding(provider, facade, domain="gating", disabled_status=404)
    await binding.start()
    yield facade
    await binding.stop()
    await registry.stop()


@feature_flag("on")
def sync_on(x: int) -> int:
    return x + 1


@feature_flag("off")
async def async_off(x: int) -> int:
    return x + 1


@feature_flag("missing")
def missing_closed() -> str:
    return "ran"


@feature_flag("missing", default=True)
def missing_open() -> str:
    return "ran"


@feature_flag("paused")
def paused() -> str:
    return "ran"


@feature_flag("checkout", variant="treatment")
async def treatment() -> str:
    return "treatment"


@feature_flag("checkout", variant="control", fallback=lambda: "fallback")
def control() -> str:
    return "control"


async def test_functions_run_only_when_their_flag_is_on(flags: FeatureFlags) -> None:
    assert sync_on(1) == 2
    with pytest.raises(FeatureFlagNotFoundException) as raised:
        await async_off(1)
    assert raised.value.key == "off" and raised.value.code == "FEATURE_FLAG_DISABLED"
    assert raised.value.context == {"flag": "off"}


async def test_missing_flags_fail_closed_unless_default_is_true(flags: FeatureFlags) -> None:
    with pytest.raises(FeatureFlagDisabledException):
        missing_closed()
    assert missing_open() == "ran"
    with pytest.raises(FeatureFlagDisabledException):
        paused()  # a DISABLED flag returns the caller default: closed


async def test_variant_gates_and_callable_fallbacks(flags: FeatureFlags) -> None:
    assert await treatment() == "treatment"
    assert control() == "fallback"


async def test_variant_gates_use_caller_default_when_no_variant_is_resolved(flags: FeatureFlags) -> None:
    @feature_flag("paused", variant="on", default=True)
    def paused_sync() -> str:
        return "open"

    @feature_flag("no-default", variant="on", default=True)
    async def no_default_async() -> str:
        return "open"

    @feature_flag("paused", variant="on")
    async def paused_closed() -> str:
        return "closed"

    assert paused_sync() == "open"
    assert await no_default_async() == "open"
    with pytest.raises(FeatureFlagDisabledException):
        await paused_closed()


def test_without_a_running_application_every_gate_is_closed_or_default() -> None:
    with pytest.raises(FeatureFlagDisabledException):
        sync_on(1)
    assert missing_open() == "ran"


class Checkout:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @feature_flag("off", fallback="legacy")
    def pay(self, amount: int) -> str:
        return f"new:{amount}"

    def legacy(self, amount: int) -> str:
        return f"legacy:{amount}"

    @feature_flag("off", fallback="legacy_async")
    async def pay_async(self, amount: int) -> str:
        return f"new:{amount}"

    async def legacy_async(self, amount: int) -> str:
        return f"legacy:{amount}"


async def test_a_method_falls_back_to_a_method_of_the_same_object(flags: FeatureFlags) -> None:
    checkout = Checkout()
    assert checkout.pay(5) == "legacy:5"
    assert await checkout.pay_async(7) == "legacy:7"


@feature_flag("off")
class Beta:
    def run(self) -> str:
        return "run"

    async def run_async(self) -> str:
        return "run"

    def _private(self) -> str:
        return "private"

    @staticmethod
    def tool() -> str:
        return "tool"


async def test_the_class_form_gates_every_public_method(flags: FeatureFlags) -> None:
    beta = Beta()
    with pytest.raises(FeatureFlagDisabledException):
        beta.run()
    with pytest.raises(FeatureFlagDisabledException):
        await beta.run_async()
    with pytest.raises(FeatureFlagDisabledException):
        Beta.tool()
    assert beta._private() == "private"


async def test_class_gate_covers_inherited_methods_without_changing_the_parent(flags: FeatureFlags) -> None:
    class Parent:
        def inherited(self) -> str:
            return "parent"

        async def inherited_async(self) -> str:
            return "parent-async"

        @staticmethod
        def static() -> str:
            return "static"

        @classmethod
        def class_method(cls) -> str:
            return cls.__name__

        def with_arg(self, value: str) -> str:
            return value

        partial = partialmethod(with_arg, "partial")

        async def with_arg_async(self, value: str) -> str:
            return value

        partial_async = partialmethod(with_arg_async, "partial-async")

    @feature_flag("off")
    class Child(Parent):
        def inherited(self) -> str:
            return "child"

    @feature_flag("on")
    class OpenChild(Parent):
        def inherited(self) -> str:
            return "child"

    child = Child()
    for call in (child.inherited, child.static, Child.class_method):
        with pytest.raises(FeatureFlagDisabledException):
            call()
    with pytest.raises(FeatureFlagDisabledException):
        await child.inherited_async()
    with pytest.raises(FeatureFlagDisabledException):
        child.partial()
    with pytest.raises(FeatureFlagDisabledException):
        await child.partial_async()
    assert Parent().inherited() == "parent"
    assert await Parent().inherited_async() == "parent-async"
    assert Parent.static() == "static"
    assert Parent.class_method() == "Parent"
    assert OpenChild().inherited() == "child"
    assert await OpenChild().inherited_async() == "parent-async"
    assert OpenChild.static() == "static"
    assert OpenChild.class_method() == "OpenChild"
    assert OpenChild().partial() == "partial"
    assert await OpenChild().partial_async() == "partial-async"


async def test_class_named_fallback_uses_the_original_public_method(flags: FeatureFlags) -> None:
    @feature_flag("off", fallback="legacy")
    class CheckoutWithFallback:
        def pay(self, amount: int) -> str:
            return f"new:{amount}"

        async def pay_async(self, amount: int) -> str:
            return f"new:{amount}"

        async def legacy(self, amount: int) -> str:
            return f"legacy:{amount}"

    checkout = CheckoutWithFallback()
    assert await checkout.pay_async(7) == "legacy:7"

    @feature_flag("off", fallback="legacy")
    class SyncCheckout:
        def pay(self, amount: int) -> str:
            return f"new:{amount}"

        def legacy(self, amount: int) -> str:
            return f"legacy:{amount}"

    assert SyncCheckout().pay(5) == "legacy:5"

    class LegacyBase:
        def legacy(self, amount: int) -> str:
            return f"inherited:{amount}"

    @feature_flag("off", fallback="legacy")
    class InheritedFallback(LegacyBase):
        def pay(self, amount: int) -> str:
            return f"new:{amount}"

    assert InheritedFallback().pay(9) == "inherited:9"


def test_the_wrapper_keeps_the_signature_and_the_kind() -> None:
    assert inspect.signature(sync_on) == inspect.signature(sync_on.__wrapped__)  # type: ignore[attr-defined]
    assert inspect.iscoroutinefunction(async_off) and not inspect.iscoroutinefunction(sync_on)
    assert sync_on.__name__ == "sync_on"


def test_an_invalid_key_is_refused_at_decoration() -> None:
    with pytest.raises(ValueError, match="invalid flag key"):
        feature_flag("bad key")


@pytest.mark.parametrize(
    ("status", "kernel", "concrete"),
    [
        (404, ResourceNotFoundException, FeatureFlagNotFoundException),
        (403, ForbiddenException, FeatureFlagForbiddenException),
        (503, ServiceUnavailableException, FeatureFlagUnavailableException),
    ],
)
def test_the_disabled_exception_carries_the_configured_status(
    status: int, kernel: type[Exception], concrete: type[Exception]
) -> None:
    error = feature_flag_disabled("k", status)
    assert isinstance(error, kernel) and isinstance(error, concrete) and isinstance(error, FeatureFlagDisabledException)
    with pytest.raises(ValueError, match="404, 403 or 503"):
        feature_flag_disabled("k", 410)


@rest_controller
@request_mapping("/beta")
class BetaController:
    @get_mapping("/items/{item_id}")
    @feature_flag("beta-items")
    async def item(self, item_id: PathVar[int]) -> dict[str, int]:
        return {"item": item_id}

    @feature_flag("beta-items")
    @get_mapping("/plain")
    def plain(self) -> dict[str, str]:
        return {"plain": "yes"}


async def _route_status(flags: dict[str, Any], status: int) -> tuple[int, int, dict[str, Any]]:
    config = Config(
        {"pyfly": {"feature-flags": {"enabled": "true", "flags": flags, "web": {"disabled-status": status}}}}
    )
    context = ApplicationContext(config)
    context.register_bean(BetaController)
    await context.start()
    app = create_app(context=context, actuator_enabled=False, docs_enabled=False)
    # Starlette's ServerErrorMiddleware sends the handler's response, then re-raises for the server to log it.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        item = await client.get("/beta/items/42")
        plain = await client.get("/beta/plain")
    await context.stop()
    return item.status_code, plain.status_code, item.json()


async def test_a_gated_route_keeps_its_mapping_and_its_parameters() -> None:
    item, plain, body = await _route_status({"beta-items": True}, 404)
    assert (item, plain, body) == (200, 200, {"item": 42})


@pytest.mark.parametrize("status", [404, 403, 503])
async def test_a_disabled_route_answers_the_configured_status(status: int) -> None:
    item, plain, body = await _route_status({"beta-items": False}, status)
    assert (item, plain) == (status, status)
    assert body["error"]["code"] == "FEATURE_FLAG_DISABLED"


def test_the_gate_imports_without_openfeature() -> None:
    """Review focus 4: the decorator is importable (and closed) without the feature-flags extra."""
    code = (
        "import sys; sys.modules['openfeature'] = None; "
        "from pyfly.feature_flags.gating import feature_flag, FeatureFlagDisabledException\n"
        "@feature_flag('x', default=True)\ndef f():\n    return 'ran'\nprint(f())"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran"
