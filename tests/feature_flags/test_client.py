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
"""The FeatureFlags facade and the OpenFeature binding (spec 5 "OpenFeature wiring", 6.2 client.py)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.evaluation_context import EvaluationContext
from openfeature.flag_evaluation import FlagEvaluationDetails, FlagValueType, Reason
from openfeature.hook import Hook, HookContext, HookHints
from openfeature.provider import AbstractProvider
from openfeature.provider._registry import provider_registry
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from openfeature.transaction_context import ContextVarsTransactionContextPropagator, NoOpTransactionContextPropagator

from pyfly.container.container import Container
from pyfly.feature_flags.client import (
    FIREFLY_CLIENT_DOMAIN,
    PREVIEW_HINT,
    FeatureFlags,
    OpenFeatureBinding,
    client_domain,
    find_external_provider,
    typed_default,
)
from pyfly.feature_flags.context import EvaluationContextResolver
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FeatureFlagsError, FlagRegistry
from pyfly.feature_flags.slot import installed_feature_flags
from tests.feature_flags.support import StaticSource, bool_flag, wait_until

FLAGS: dict[str, Any] = {
    "beta-ui": {**bool_flag("off"), "targeting": {"if": [{"==": [{"var": "plan"}, "pro"]}, "on", None]}},
    "theme": "dark",
    "page-size": {"state": "ENABLED", "variants": {"small": 10, "large": 50}, "defaultVariant": "large"},
    "ratio": {"state": "ENABLED", "variants": {"low": 0.1, "high": 1}, "defaultVariant": "low"},
    "banner": {"state": "ENABLED", "variants": {"plain": {"title": "Hi"}}, "defaultVariant": "plain"},
    "by-user": {**bool_flag("off"), "targeting": {"if": [{"==": [{"var": "targetingKey"}, "u-1"]}, "on", None]}},
    "by-caller": {**bool_flag("off"), "targeting": {"if": [{"==": [{"var": "targetingKey"}, "caller"]}, "on", None]}},
    "rollout": {**bool_flag("off"), "targeting": {"fractional": [["on", 50], ["off", 50]]}},
}


@pytest.fixture(autouse=True)
def _no_transaction_context() -> Iterator[None]:
    """Every test starts without a transaction context: an earlier test may have left one in the SDK's ContextVar
    (it is a class attribute, shared by every ``ContextVarsTransactionContextPropagator``)."""
    variable = ContextVarsTransactionContextPropagator._transaction_context_var  # noqa: SLF001
    token = variable.set(None)
    yield
    variable.reset(token)


class FreePlan:
    def contribute(self, attributes: dict[str, Any]) -> None:
        attributes["plan"] = "free"
        attributes["targetingKey"] = "ambient-user"


class Hints(Hook):
    def __init__(self) -> None:
        self.hints: list[dict[str, Any]] = []

    def finally_after(
        self, hook_context: HookContext, details: FlagEvaluationDetails[FlagValueType], hints: HookHints
    ) -> None:
        self.hints.append(dict(hints))


class Boom(BaseException):
    """Escapes the OpenFeature client, which turns every ``Exception`` into an error result."""


class Contexts(Hook):
    """Records the merged evaluation context every evaluation runs with; raises :class:`Boom` after it when told."""

    def __init__(self, *, fail: bool = False) -> None:
        self.contexts: list[EvaluationContext] = []
        self.fail = fail

    def before(self, hook_context: HookContext, hints: HookHints) -> EvaluationContext | None:
        self.contexts.append(hook_context.evaluation_context)
        return None

    def after(self, hook_context: HookContext, details: FlagEvaluationDetails[FlagValueType], hints: HookHints) -> None:
        if self.fail:
            raise Boom


async def _bound(
    domain: str = "facade-test", hooks: list[Hook] | None = None
) -> tuple[FeatureFlags, OpenFeatureBinding]:
    provider = FireflyFlagProvider()
    registry = FlagRegistry([StaticSource("config", FLAGS)], provider)
    await registry.start()
    client = OpenFeatureClient(domain=domain, version=None, hooks=hooks or [])
    facade = FeatureFlags(client, EvaluationContextResolver(contributors=[FreePlan()]), registry=registry)
    binding = OpenFeatureBinding(provider, facade, domain=domain, disabled_status=503)
    await binding.start()
    return facade, binding


def _propagator() -> object:
    import openfeature.transaction_context as transaction_context

    return transaction_context._evaluation_transaction_context_propagator  # noqa: SLF001


async def test_typed_getters_and_their_async_twins() -> None:
    flags, binding = await _bound()
    assert flags.is_enabled("beta-ui") is False and await flags.is_enabled_async("beta-ui") is False
    assert flags.get_string("theme", "light") == "dark" == await flags.get_string_async("theme", "light")
    assert flags.get_int("page-size", 1) == 50 == await flags.get_int_async("page-size", 1)
    assert flags.get_float("ratio", 9.9) == 0.1 == await flags.get_float_async("ratio", 9.9)
    assert flags.get_object("banner", {}) == {"title": "Hi"} == await flags.get_object_async("banner", {})
    assert flags.is_enabled("missing", default=True) is True
    await binding.stop()


async def test_typed_getters_evaluate_with_their_own_type_whatever_the_defaults_runtime_type() -> None:
    flags, binding = await _bound()
    # an int default does not turn a float flag into an integer evaluation (TYPE_MISMATCH -> the default)
    assert flags.get_float("ratio", 1) == 0.1 == await flags.get_float_async("ratio", 1)
    # a bool default (a bool is an int) does not turn an integer flag into a boolean evaluation
    assert flags.get_int("page-size", True) == 50 == await flags.get_int_async("page-size", True)
    # the default is converted to the getter's type: int(True) is 1, float(2) is 2.0
    for value, expected in [
        (flags.get_int("missing", True), 1),
        (await flags.get_int_async("missing", True), 1),
        (flags.get_float("missing", 2), 2.0),
        (await flags.get_float_async("missing", 2), 2.0),
        (flags.get_float("missing", True), 1.0),
    ]:
        assert value == expected and type(value) is type(expected)
    await binding.stop()


async def test_explicit_context_wins_over_the_ambient_one() -> None:
    flags, binding = await _bound()
    assert flags.is_enabled("beta-ui") is False  # ambient plan=free
    assert flags.is_enabled("beta-ui", context={"plan": "pro"}) is True
    assert flags.is_enabled("by-user") is False  # ambient targetingKey=ambient-user
    assert flags.is_enabled("by-user", targeting_key="u-1") is True
    assert flags.is_enabled("by-user", context={"targetingKey": "u-1"}) is True
    assert flags.evaluation_context(ambient=False).targeting_key is None
    await binding.stop()


async def test_integer_targeting_key_uses_the_same_fractional_bucket_as_decimal_text() -> None:
    flags, binding = await _bound()
    try:
        integer = flags.evaluation_context({"targetingKey": 42})
        text = flags.evaluation_context({"targetingKey": "42"})
        assert integer.targeting_key == text.targeting_key == "42"
        assert flags.variant("rollout", context={"targetingKey": 42}) == flags.variant(
            "rollout", context={"targetingKey": "42"}
        )
        assert flags.evaluation_context({"targetingKey": True}).targeting_key == "ambient-user"
        assert flags.evaluation_context({"targetingKey": 42.0}).targeting_key == "ambient-user"
    finally:
        await binding.stop()


async def test_variant_evaluates_with_the_flags_own_type() -> None:
    flags, binding = await _bound()
    assert flags.variant("page-size") == "large" == await flags.variant_async("page-size")
    assert flags.variant("ratio") == "low"
    assert flags.variant("banner") == "plain"
    assert flags.variant("beta-ui", context={"plan": "pro"}) == "on"
    assert flags.variant("missing") is None
    assert flags.variant_details("missing").error_code is not None
    await binding.stop()


async def test_preview_evaluations_carry_the_preview_hint() -> None:
    hints = Hints()
    flags, binding = await _bound(hooks=[hints])
    flags.details("theme", "x")
    flags.details("theme", "x", preview=True)
    assert hints.hints == [{}, {PREVIEW_HINT: True}]
    await binding.stop()


async def test_a_non_ambient_evaluation_ignores_the_callers_transaction_context() -> None:
    contexts = Contexts()
    flags, binding = await _bound(hooks=[contexts])
    caller = EvaluationContext(targeting_key="caller", attributes={"plan": "pro"})
    api.set_transaction_context(caller)
    # the transaction context reaches every evaluation the SDK makes: a plain client call targets the caller
    assert flags.client.get_boolean_details("beta-ui", False).variant == "on"
    assert flags.client.get_boolean_details("by-caller", False).variant == "on"
    previews = [
        flags.details("beta-ui", False, context={"region": "eu"}, ambient=False),
        await flags.details_async("beta-ui", False, context={"region": "eu"}, ambient=False),
        flags.details("by-caller", False, context={"region": "eu"}, ambient=False),
        await flags.details_async("by-caller", False, context={"region": "eu"}, ambient=False),
    ]
    for details in previews:
        assert (details.value, details.variant, details.reason) == (False, "off", Reason.DEFAULT)
    for seen in contexts.contexts[2:]:
        assert seen.targeting_key is None and seen.attributes == {"region": "eu"}
    assert api.get_transaction_context() is caller
    await binding.stop()


async def test_a_failing_non_ambient_evaluation_restores_the_transaction_context() -> None:
    contexts = Contexts(fail=True)
    flags, binding = await _bound(hooks=[contexts])
    caller = EvaluationContext(targeting_key="caller", attributes={"plan": "pro"})
    api.set_transaction_context(caller)
    with pytest.raises(Boom):
        flags.details("beta-ui", False, ambient=False)
    assert api.get_transaction_context() is caller
    with pytest.raises(Boom):
        await flags.details_async("beta-ui", False, ambient=False)
    assert api.get_transaction_context() is caller
    assert [seen.targeting_key for seen in contexts.contexts] == [None, None]
    await binding.stop()


async def test_the_binding_installs_the_provider_propagator_and_slot_and_restores_them() -> None:
    flags, binding = await _bound()
    assert api.get_provider_metadata("facade-test").name == "firefly"
    assert flags.client.get_provider_status().value == "READY"
    installed = installed_feature_flags()
    assert installed is not None and installed.facade is flags and installed.disabled_status == 503
    await binding.stop()
    assert api.get_provider_metadata("facade-test").name == "No-op Provider"
    assert installed_feature_flags() is None
    import openfeature.transaction_context as transaction_context

    propagator = transaction_context._evaluation_transaction_context_propagator  # noqa: SLF001
    assert isinstance(propagator, NoOpTransactionContextPropagator)


async def test_a_binding_never_uninstalls_what_another_binding_installed() -> None:
    first, first_binding = await _bound()
    second, second_binding = await _bound()  # same domain: replaces the first
    await first_binding.stop()
    assert api.get_provider_metadata("facade-test").name == "firefly"
    assert OpenFeatureClient(domain="facade-test", version=None).provider is second_binding.provider
    installed = installed_feature_flags()
    assert installed is not None and installed.facade is second
    await second_binding.stop()
    assert installed_feature_flags() is None


class Draining(Hook):
    """A hook with buffered work (the exposure hook's shape): the binding drains it on stop."""

    def __init__(self, *, fail: bool = False) -> None:
        self.drained = 0
        self.fail = fail

    async def drain(self) -> None:
        self.drained += 1
        if self.fail:
            raise RuntimeError("drain failed")


async def test_stop_drains_the_hooks_and_a_failing_drain_does_not_stop_the_restore(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing, healthy = Draining(fail=True), Draining()
    flags, binding = await _bound(hooks=[failing, healthy])
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.stop()
    assert (failing.drained, healthy.drained) == (1, 1)
    assert [record.getMessage() for record in caplog.records] == ["feature_flags_hook_drain_failed"]
    assert api.get_provider_metadata("facade-test").name == "No-op Provider"
    assert installed_feature_flags() is None
    assert isinstance(_propagator(), NoOpTransactionContextPropagator)


class SyncDrain(Hook):
    """A third-party hook with a synchronous ``drain()``: not the exposure hook's coroutine, so never called."""

    def __init__(self) -> None:
        self.drained = 0

    def drain(self) -> None:
        self.drained += 1


async def test_stop_awaits_only_coroutine_drains(caplog: pytest.LogCaptureFixture) -> None:
    sync_drain, async_drain = SyncDrain(), Draining()
    flags, binding = await _bound(hooks=[sync_drain, async_drain])
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.stop()
    assert (sync_drain.drained, async_drain.drained) == (0, 1)
    assert caplog.records == []


class BlockingDrain(Hook):
    """A drain that waits until it is cancelled while *block* holds (a stop that times out)."""

    def __init__(self) -> None:
        self.entered = 0
        self.block = True

    async def drain(self) -> None:
        self.entered += 1
        if self.block:
            await asyncio.Event().wait()  # never set: only a cancellation ends it


async def test_a_stop_cancelled_while_draining_still_restores_and_the_binding_starts_again() -> None:
    found = NoOpTransactionContextPropagator()
    api.set_transaction_context_propagator(found)
    hook = BlockingDrain()
    flags, binding = await _bound(hooks=[hook])
    stopping = asyncio.create_task(binding.stop())
    await wait_until(lambda: hook.entered == 1)
    stopping.cancel()  # the context's stop timeout
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert api.get_provider_metadata("facade-test").name == "No-op Provider"
    assert _propagator() is found
    assert installed_feature_flags() is None
    hook.block = False
    await binding.start()
    assert isinstance(_propagator(), ContextVarsTransactionContextPropagator)
    assert flags.get_string("theme", "light") == "dark"
    await binding.stop()
    assert hook.entered == 2
    assert api.get_provider_metadata("facade-test").name == "No-op Provider"
    assert _propagator() is found  # the restart did not record its own stale propagator as the one it found


class FailingProvider(InMemoryProvider):
    """An external provider whose first *failures* initializations fail."""

    def __init__(self, failures: int) -> None:
        super().__init__({"ext": InMemoryFlag("on", {"on": "yes", "off": "no"})})
        self.failures = failures

    def initialize(self, evaluation_context: EvaluationContext) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("provider unavailable")


async def test_stop_undoes_a_start_that_failed() -> None:
    found = NoOpTransactionContextPropagator()
    api.set_transaction_context_propagator(found)
    provider = FailingProvider(failures=1)
    facade = FeatureFlags(OpenFeatureClient(domain="failing", version=None), EvaluationContextResolver())
    binding = OpenFeatureBinding(provider, facade, domain="failing")
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await binding.start()
    assert facade.client.provider is provider  # the SDK binds the provider before it initializes it
    await binding.stop()  # what the context does after a failed start
    assert api.get_provider_metadata("failing").name == "No-op Provider"
    assert _propagator() is found
    assert installed_feature_flags() is None
    await binding.start()  # the provider initializes this time
    assert facade.get_string("ext", "x") == "yes"
    await binding.stop()
    assert api.get_provider_metadata("failing").name == "No-op Provider"
    assert _propagator() is found


async def test_a_binding_without_a_provider_warns_and_installs_the_rest(caplog: pytest.LogCaptureFixture) -> None:
    facade = FeatureFlags(OpenFeatureClient(domain="no-provider", version=None), EvaluationContextResolver())
    binding = OpenFeatureBinding(None, facade, domain="no-provider")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
    assert [(record.getMessage(), getattr(record, "domain", None)) for record in caplog.records] == [
        ("feature_flags_no_provider", "no-provider")
    ]
    assert binding.provider is None
    assert api.get_provider_metadata("no-provider").name == "No-op Provider"
    assert facade.is_enabled("anything", default=True) is True  # the no-op provider answers the default
    installed = installed_feature_flags()
    assert installed is not None and installed.facade is facade
    assert isinstance(_propagator(), ContextVarsTransactionContextPropagator)
    await binding.stop()
    assert installed_feature_flags() is None
    assert isinstance(_propagator(), NoOpTransactionContextPropagator)


async def test_a_stopped_binding_starts_again_and_restores_what_it_found_each_time() -> None:
    found = NoOpTransactionContextPropagator()  # a propagator someone installed before the context started
    api.set_transaction_context_propagator(found)
    flags, binding = await _bound()
    for _ in range(2):
        assert isinstance(_propagator(), ContextVarsTransactionContextPropagator)
        await binding.stop()
        assert api.get_provider_metadata("facade-test").name == "No-op Provider"
        assert installed_feature_flags() is None
        assert _propagator() is found
        await binding.start()  # a cold restart of the context
        assert api.get_provider_metadata("facade-test").name == "firefly"
        assert flags.client.get_provider_status().value == "READY"
        assert flags.get_string("theme", "light") == "dark"
        installed = installed_feature_flags()
        assert installed is not None and installed.facade is flags and installed.disabled_status == 503
    await binding.stop()
    assert _propagator() is found


async def test_start_and_stop_are_idempotent() -> None:
    hook = Draining()
    flags, binding = await _bound(hooks=[hook])
    await binding.start()  # a second start must not take its own propagator for the one it found
    await binding.stop()
    await binding.stop()
    assert hook.drained == 1  # only a running binding drains
    assert api.get_provider_metadata("facade-test").name == "No-op Provider"
    assert installed_feature_flags() is None
    assert isinstance(_propagator(), NoOpTransactionContextPropagator)


async def test_the_default_domain_installs_the_default_provider() -> None:
    provider = FireflyFlagProvider()
    provider.update({"flags": {"a": bool_flag()}})
    facade = FeatureFlags(
        OpenFeatureClient(domain=client_domain(""), version=None), EvaluationContextResolver(), registry=None
    )
    binding = OpenFeatureBinding(provider, facade, domain=None)
    await binding.start()
    assert client_domain("") == FIREFLY_CLIENT_DOMAIN and client_domain("payments") == "payments"
    assert api.get_client().get_boolean_value("a", False) is True  # third-party default client
    assert facade.is_enabled("a") is True  # the "firefly" domain falls back to the default provider
    await binding.stop()
    assert api.get_provider_metadata().name == "No-op Provider"


async def test_an_external_provider_serves_the_facade() -> None:
    external = InMemoryProvider({"ext": InMemoryFlag("on", {"on": "yes", "off": "no"})})
    facade = FeatureFlags(OpenFeatureClient(domain="ext", version=None), EvaluationContextResolver())
    binding = OpenFeatureBinding(external, facade, domain="ext")
    await binding.start()
    assert facade.get_string("ext", "x") == "yes"
    assert facade.variant("ext") == "on"  # no registry: string evaluation
    await binding.stop()


def test_find_external_provider_scans_the_container() -> None:
    container = Container()
    assert find_external_provider(container) is None
    external = InMemoryProvider({})
    container.register_instance(InMemoryProvider, external)
    assert find_external_provider(container) is external
    assert isinstance(external, AbstractProvider)


class OtherProvider(InMemoryProvider):
    pass


def test_find_external_provider_refuses_several_provider_beans() -> None:
    container = Container()
    container.register_instance(FireflyFlagProvider, FireflyFlagProvider())  # Firefly's own is never external
    external = InMemoryProvider({})
    container.register_instance(InMemoryProvider, external)
    container.register_instance(AbstractProvider, external)  # one bean under a second type is one provider
    assert find_external_provider(container) is external
    container.register_instance(OtherProvider, OtherProvider({}))
    expected = "expected a single OpenFeature provider bean but found 2: InMemoryProvider, OtherProvider"
    with pytest.raises(FeatureFlagsError, match=expected):
        find_external_provider(container)


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (bool_flag(), False),
        ({"state": "ENABLED", "variants": {"a": "x"}}, ""),
        ({"state": "ENABLED", "variants": {"a": 1, "b": 2}}, 0),
        ({"state": "ENABLED", "variants": {"a": 1, "b": 2.5}}, 0.0),
        ({"state": "ENABLED", "variants": {"a": {"k": 1}}}, {}),
    ],
)
def test_typed_default(definition: dict[str, Any], expected: object) -> None:
    value = typed_default(definition)
    assert value == expected and type(value) is type(expected)


# -- installing over a provider that is not the binding's own --------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[tuple[str, Any, Any]]:
    return [
        (record.getMessage(), getattr(record, "domain", None), getattr(record, "provider", None))
        for record in caplog.records
        if record.name == "pyfly.feature_flags.client" and record.levelno == logging.WARNING
    ]


def _default_domain_binding() -> tuple[FeatureFlags, OpenFeatureBinding]:
    provider = FireflyFlagProvider()
    provider.update({"flags": {"a": bool_flag()}})
    facade = FeatureFlags(OpenFeatureClient(domain=client_domain(""), version=None), EvaluationContextResolver())
    return facade, OpenFeatureBinding(provider, facade, domain=None)


async def test_two_contexts_on_the_default_domain_are_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    _, first = _default_domain_binding()
    _, second = _default_domain_binding()
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await first.start()
        await second.start()  # replaces the first context's provider for every default-domain client
    assert _warnings(caplog) == [("feature_flags_provider_replaced", None, "FireflyFlagProvider")]
    await second.stop()
    await first.stop()


async def test_installing_over_an_application_provider_in_a_named_domain_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api.set_provider_and_wait(InMemoryProvider({}), "payments")
    facade = FeatureFlags(OpenFeatureClient(domain="payments", version=None), EvaluationContextResolver())
    binding = OpenFeatureBinding(FireflyFlagProvider(), facade, domain="payments")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
    assert _warnings(caplog) == [("feature_flags_provider_replaced", "payments", "InMemoryProvider")]
    await binding.stop()


async def test_a_provider_bound_to_the_firefly_domain_shadowing_the_framework_client_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The framework's client is in the ``firefly`` domain and reaches the default provider only while nothing is
    bound to that domain: an application provider bound to it would answer the facade instead of Firefly's."""
    api.set_provider_and_wait(InMemoryProvider({"a": InMemoryFlag("off", {"on": True, "off": False})}), "firefly")
    facade, binding = _default_domain_binding()
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
    assert _warnings(caplog) == [("feature_flags_client_domain_shadowed", FIREFLY_CLIENT_DOMAIN, "InMemoryProvider")]
    assert facade.is_enabled("a") is False  # what the warning is about: the application's provider answers
    await binding.stop()


async def test_installing_over_no_provider_or_over_its_own_is_not_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    facade, binding = _default_domain_binding()
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
        await binding.stop()
        await binding.start()  # a restart finds the no-op provider the stop put back
        await binding.stop()
        api.set_provider_and_wait(binding.provider, None)  # type: ignore[arg-type]  # its own provider, still there
        await binding.start()
    assert _warnings(caplog) == []
    assert facade.is_enabled("a") is True
    await binding.stop()
    assert api.get_provider_metadata().name == "No-op Provider"  # its own provider, found installed, is not put back


async def test_a_named_domain_next_to_an_application_default_provider_is_not_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The setup ``openfeature.domain`` exists for: the application's provider is the default one and Firefly serves
    a domain of its own. Nothing is bound to that domain before the binding starts, so nothing is replaced."""
    application = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(application)
    facade = FeatureFlags(OpenFeatureClient(domain="flags", version=None), EvaluationContextResolver())
    provider = FireflyFlagProvider()
    provider.update({"flags": {"a": bool_flag()}})
    binding = OpenFeatureBinding(provider, facade, domain="flags")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
    assert _warnings(caplog) == []
    assert facade.is_enabled("a") is True
    assert OpenFeatureClient(domain=None, version=None).provider is application
    await binding.stop()


# -- stop puts back what start found in the domain -----------------------------------------------------------------


def _firefly_binding(domain: str | None, key: str = "a") -> tuple[FeatureFlags, OpenFeatureBinding]:
    provider = FireflyFlagProvider()
    provider.update({"flags": {key: bool_flag()}})
    facade = FeatureFlags(
        OpenFeatureClient(domain=client_domain(domain or ""), version=None), EvaluationContextResolver()
    )
    return facade, OpenFeatureBinding(provider, facade, domain=domain)


def _bound_to(domain: str) -> object:
    """What is bound to *domain* itself (``None``: nothing, it falls back to the default provider)."""
    return provider_registry._providers.get(domain)  # noqa: SLF001 — the SDK has no public "is bound" query


async def test_stop_restores_the_application_provider_the_binding_replaced_in_its_domain() -> None:
    application = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(application, "x")
    _, binding = _firefly_binding("x")
    await binding.start()
    assert _bound_to("x") is binding.provider
    await binding.stop()
    assert _bound_to("x") is application
    assert OpenFeatureClient(domain="x", version=None).get_boolean_value("app", False) is True


async def test_stop_unbinds_a_domain_that_held_nothing_so_it_falls_back_to_the_default_provider_again() -> None:
    _, binding = _firefly_binding("x")
    await binding.start()
    await binding.stop()
    assert _bound_to("x") is None
    default = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(default)  # a default provider set afterwards serves the domain again
    assert OpenFeatureClient(domain="x", version=None).provider is default


async def test_stop_restores_the_application_default_provider_the_binding_replaced() -> None:
    application = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(application)
    _, binding = _firefly_binding(None)
    await binding.start()
    assert OpenFeatureClient(domain=None, version=None).provider is binding.provider
    await binding.stop()
    assert OpenFeatureClient(domain=None, version=None).provider is application


@pytest.mark.parametrize("order", ["last-in-first-out", "first-in-first-out"])
async def test_overlapping_bindings_leave_the_domain_as_they_found_it_whatever_order_they_stop_in(order: str) -> None:
    """A binding that stops after another one replaced its provider cannot uninstall it; the other one, stopping later,
    must then restore what the first one found, never the first one's (stopped) provider."""
    application = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(application, "x")
    _, first = _firefly_binding("x")
    _, second = _firefly_binding("x")
    await first.start()
    await second.start()
    if order == "last-in-first-out":
        await second.stop()
        assert _bound_to("x") is first.provider  # the first context still runs: its provider is back
        await first.stop()
    else:
        await first.stop()
        assert _bound_to("x") is second.provider  # the second context still runs: untouched
        await second.stop()
    assert _bound_to("x") is application


class OnceOnlyProvider(InMemoryProvider):
    """An application provider that cannot be initialized a second time (the SDK shut it down when it was replaced)."""

    def __init__(self) -> None:
        super().__init__({"app": InMemoryFlag("on", {"on": True, "off": False})})
        self.initialized = 0

    def initialize(self, evaluation_context: EvaluationContext) -> None:
        self.initialized += 1
        if self.initialized > 1:
            raise RuntimeError("already shut down")


async def test_a_provider_that_cannot_be_put_back_is_logged_and_the_stop_completes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = OnceOnlyProvider()
    api.set_provider_and_wait(application, "x")
    _, binding = _firefly_binding("x")
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.client"):
        await binding.start()
        await binding.stop()
    assert _warnings(caplog) == [
        ("feature_flags_provider_replaced", "x", "OnceOnlyProvider"),  # at start
        ("feature_flags_provider_restore_failed", "x", "OnceOnlyProvider"),
    ]
    assert application.initialized == 2
    assert installed_feature_flags() is None
    assert isinstance(_propagator(), NoOpTransactionContextPropagator)


async def test_a_restarted_binding_drops_the_record_its_earlier_stop_left() -> None:
    """The reviewer's repro: application code replaces the Firefly provider while the context runs, the context stops
    (recording what it had found) and starts again. A second context that starts and stops on the domain meanwhile
    must put the first context's (running) provider back, not what the first one had found before its restart."""
    application = InMemoryProvider({"app": InMemoryFlag("on", {"on": True, "off": False})})
    api.set_provider_and_wait(application, "x")
    _, first = _firefly_binding("x")
    _, second = _firefly_binding("x")
    await first.start()
    replacement = InMemoryProvider({})
    api.set_provider_and_wait(replacement, "x")  # application code replaces Firefly's provider mid-run
    await first.stop()  # its provider is no longer bound: it leaves the replacement alone
    assert _bound_to("x") is replacement
    await first.start()  # a context restart: it now finds the replacement
    await second.start()
    await second.stop()
    assert _bound_to("x") is first.provider  # the first context still runs
    await first.stop()
    assert _bound_to("x") is replacement
