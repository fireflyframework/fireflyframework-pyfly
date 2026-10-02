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
"""FeatureFlagsAutoConfiguration: one switch wires the provider, registry, facade, binding and filter (spec 6.1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from openfeature import api
from openfeature.client import OpenFeatureClient
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from prometheus_client import REGISTRY

from pyfly.config.auto import discover_auto_configurations
from pyfly.container.bean import bean
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.feature_flags.auto_configuration import FeatureFlagsAutoConfiguration
from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
from pyfly.feature_flags.context import EvaluationContextResolver, FeatureFlagsContextFilter
from pyfly.feature_flags.events import FeatureFlagEvaluated
from pyfly.feature_flags.hooks import EVALUATIONS_METRIC, ExposureEventHook, MetricsHook
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FeatureFlagsError, FlagRegistry
from pyfly.feature_flags.slot import installed_feature_flags
from pyfly.observability.correlation import set_tenant_id
from tests.feature_flags.support import bool_flag


def _config(**section: Any) -> Config:
    return Config({"pyfly": {"app": {"name": "shop"}, "feature-flags": {"enabled": "true", **section}}})


async def _started(config: Config, *beans: type) -> ApplicationContext:
    context = ApplicationContext(config)
    for bean_class in beans:
        context.register_bean(bean_class)
    await context.start()
    return context


def test_the_auto_configuration_has_an_entry_point() -> None:
    assert FeatureFlagsAutoConfiguration in discover_auto_configurations()


async def test_disabled_by_default() -> None:
    context = await _started(Config({}))
    with pytest.raises(NoSuchBeanError):
        context.get_bean(FeatureFlags)
    await context.stop()


async def test_one_switch_wires_the_subsystem() -> None:
    flags = {
        "new-checkout": True,
        "theme": "dark",
        "size": {"state": "ENABLED", "variants": {"s": 1}, "defaultVariant": "s"},
    }
    context = await _started(_config(flags=flags, web={"disabled-status": 503}))
    facade = context.get_bean(FeatureFlags)
    assert facade.is_enabled("new-checkout") is True
    assert facade.get_string("theme", "light") == "dark"
    assert facade.get_int("size", 0) == 1
    registry = context.get_bean(FlagRegistry)
    assert registry.effective_flag("theme") is not None and registry.started
    assert facade.registry is registry  # injected, not left None by a hint the context could not match
    assert api.get_provider_metadata().name == "firefly"
    assert api.get_client().get_boolean_value("new-checkout", False) is True  # third-party code sees it
    installed = installed_feature_flags()
    assert installed is not None and installed.facade is facade and installed.disabled_status == 503
    assert isinstance(context.get_bean(FeatureFlagsContextFilter), FeatureFlagsContextFilter)
    assert isinstance(context.get_bean(OpenFeatureBinding), OpenFeatureBinding)
    await context.stop()
    assert installed_feature_flags() is None
    assert api.get_provider_metadata().name == "No-op Provider"


async def test_the_registry_loads_before_the_binding_installs_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bean methods share their hints exactly, so the registry is created, and therefore started, first."""
    started: list[str] = []
    for lifecycle_class in (FlagRegistry, OpenFeatureBinding):
        original = lifecycle_class.start

        async def start(self: Any, _original: Any = original, _name: str = lifecycle_class.__name__) -> None:
            started.append(_name)
            await _original(self)

        monkeypatch.setattr(lifecycle_class, "start", start)
    context = await _started(_config(flags={"a": True}))
    assert started == ["FlagRegistry", "OpenFeatureBinding"]
    await context.stop()


async def test_a_configured_domain_installs_the_provider_for_that_domain_only() -> None:
    context = await _started(_config(flags={"a": True}, openfeature={"domain": "payments"}))
    assert api.get_provider_metadata("payments").name == "firefly"
    assert api.get_provider_metadata().name == "No-op Provider"
    assert context.get_bean(FeatureFlags).is_enabled("a") is True
    await context.stop()


async def test_placeholder_syntax_in_flag_definitions_is_flag_text() -> None:
    """``${...}`` inside ``flags``/``evaluators`` is not a configuration placeholder: the flags boot and evaluate to
    the literal text (there is no escape syntax, so resolving it would refuse a valid definition)."""
    flags = {
        "greeting": {"state": "ENABLED", "variants": {"hello": "Hello ${name}"}, "defaultVariant": "hello"},
        "dollar-note": {**bool_flag("off"), "targeting": {"if": [{"$ref": "mentions-placeholder"}, "on", "off"]}},
    }
    evaluators = {"mentions-placeholder": {"in": ["${", {"var": "note"}]}}
    context = await _started(_config(flags=flags, evaluators=evaluators))
    facade = context.get_bean(FeatureFlags)
    assert facade.get_string("greeting", "") == "Hello ${name}"
    assert facade.is_enabled("dollar-note", context={"note": "see ${x}"}) is True
    assert facade.is_enabled("dollar-note", context={"note": "plain"}) is False
    await context.stop()


async def test_an_invalid_config_definition_fails_startup_with_the_key_and_the_reason() -> None:
    with pytest.raises(BeanCreationException, match=r"bad key.*invalid flag key"):
        await _started(_config(flags={"bad key": True}))


async def test_an_expired_flag_is_logged_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    flags = {"old": bool_flag(metadata={"expires": "2020-01-01", "owner": "web"})}
    with caplog.at_level(logging.WARNING, logger="pyfly.feature_flags.registry"):
        context = await _started(_config(flags=flags))
    assert [r.flag for r in caplog.records if r.getMessage() == "feature_flag_expired"] == ["old"]  # type: ignore[attr-defined]
    await context.stop()


@configuration
class ExternalProviderConfiguration:
    @bean
    def launch_darkly_stand_in(self) -> InMemoryProvider:
        return InMemoryProvider({"ext": InMemoryFlag("on", {"on": True, "off": False})})


async def test_an_application_provider_replaces_firefly_s() -> None:
    labels = {"flag": "ext", "variant": "on", "reason": "STATIC"}
    before = REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) or 0.0
    context = await _started(_config(flags={"ext": False}, events={"evaluations": True}), ExternalProviderConfiguration)
    with pytest.raises(NoSuchBeanError):
        context.get_bean(FlagRegistry)
    facade = context.get_bean(FeatureFlags)
    assert facade.registry is None
    assert facade.is_enabled("ext") is True  # the external provider answers, not the config layer
    assert api.get_provider_metadata().name == "In-Memory Provider"
    # spec 5: the gates and the Firefly hooks work the same over the application's provider
    installed = installed_feature_flags()
    assert installed is not None and installed.facade is facade
    assert [type(hook) for hook in facade.client.hooks] == [MetricsHook, ExposureEventHook]
    assert REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) == before + 1
    await context.stop()


async def test_the_server_cannot_start_without_a_token() -> None:
    with pytest.raises(BeanCreationException, match="server.token"):
        await _started(_config(server={"enabled": True}))


# -- the sources -----------------------------------------------------------------------------------------------


async def test_the_file_source_layers_over_the_config_flags(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    path.write_text(json.dumps({"flags": {"theme": bool_flag("off") | {"variants": {"on": "dark", "off": "light"}}}}))
    file = {"enabled": True, "path": str(path), "refresh-interval": "250ms"}
    context = await _started(_config(flags={"theme": "dark", "beta": True}, sources={"file": file}))
    registry = context.get_bean(FlagRegistry)
    assert [(status.name, status.status) for status in registry.sources()] == [("config", "UP"), ("file", "UP")]
    facade = context.get_bean(FeatureFlags)
    assert facade.get_string("theme", "") == "light"  # the file layer wins over the config layer
    assert facade.is_enabled("beta") is True
    await context.stop()


async def test_a_refresh_interval_that_is_not_positive_fails_startup_with_the_full_key(tmp_path: Path) -> None:
    file = {"enabled": True, "path": str(tmp_path / "flags.json"), "refresh-interval": "0s"}
    with pytest.raises(
        BeanCreationException, match=r"pyfly\.feature-flags\.sources\.file\.refresh-interval must be greater than 0"
    ):
        await _started(_config(sources={"file": file}))


# -- the evaluation context ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("context_section", "tenant"), [({}, None), ({"trust-tenant-header": "true"}, "acme")])
async def test_the_tenant_header_is_trusted_only_when_configured(
    context_section: dict[str, Any], tenant: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYFLY_PROFILES_ACTIVE", raising=False)  # it would win over pyfly.profiles.active
    context = await _started(
        Config(
            {
                "pyfly": {
                    "app": {"name": "shop"},
                    "profiles": {"active": "dev,eu"},
                    "feature-flags": {"enabled": "true", "context": context_section},
                }
            }
        )
    )
    token = set_tenant_id("acme")  # what the request filter binds from X-Tenant-Id
    try:
        attributes = context.get_bean(EvaluationContextResolver).resolve().attributes
    finally:
        token.var.reset(token)
    assert attributes.get("tenant") == tenant
    assert (attributes["application"], attributes["profiles"]) == ("shop", ["dev", "eu"])
    await context.stop()


# -- the hooks -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("evaluations", [False, True])
async def test_exposure_events_are_published_only_when_enabled(evaluations: bool) -> None:
    context = await _started(_config(flags={"exposed": True}, events={"evaluations": evaluations}))
    hooks = context.get_bean(OpenFeatureClient).hooks
    assert [type(hook) for hook in hooks] == ([MetricsHook, ExposureEventHook] if evaluations else [MetricsHook])
    published: list[FeatureFlagEvaluated] = []

    async def record(event: FeatureFlagEvaluated) -> None:
        published.append(event)

    context.event_bus.subscribe(FeatureFlagEvaluated, record)
    assert await context.get_bean(FeatureFlags).is_enabled_async("exposed") is True
    await context.stop()  # the binding drains the exposure hook
    assert [(event.key, event.variant) for event in published] == ([("exposed", "on")] if evaluations else [])


async def test_the_metrics_hook_counts_through_the_registry_found_at_evaluation_time() -> None:
    """The ``MetricsRegistry`` bean comes from an auto-configuration processed after this one: it is looked up when
    an evaluation is counted, never injected."""
    labels = {"flag": "counted-by-auto-config", "variant": "on", "reason": "STATIC"}
    before = REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) or 0.0
    context = await _started(_config(flags={"counted-by-auto-config": True}))
    assert context.get_bean(FeatureFlags).is_enabled("counted-by-auto-config") is True
    assert REGISTRY.get_sample_value(EVALUATIONS_METRIC, labels) == before + 1
    await context.stop()


# -- startup failures are not swallowed --------------------------------------------------------------------------


class _FirstProvider(InMemoryProvider):
    pass


class _SecondProvider(InMemoryProvider):
    pass


@configuration
class TwoProvidersConfiguration:
    @bean
    def first_provider(self) -> _FirstProvider:
        return _FirstProvider({})

    @bean
    def second_provider(self) -> _SecondProvider:
        return _SecondProvider({})


async def test_several_application_providers_fail_startup_naming_them() -> None:
    with pytest.raises(BeanCreationException, match=r"expected a single OpenFeature provider bean but found 2") as info:
        await _started(_config(), TwoProvidersConfiguration)
    assert isinstance(info.value.__cause__, FeatureFlagsError)
    assert "_FirstProvider" in str(info.value) and "_SecondProvider" in str(info.value)
    assert installed_feature_flags() is None


async def test_a_refused_boot_composition_fails_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: FireflyFlagProvider, document: Any, *, changed_keys: Any = None) -> list[str]:
        raise ValueError("flagd-core said no")

    monkeypatch.setattr(FireflyFlagProvider, "update", refuse)
    refused = "refused the startup composition: ValueError: flagd-core said no"
    with pytest.raises(BeanCreationException, match=refused) as info:
        await _started(_config(flags={"a": True}))
    assert isinstance(info.value.__cause__, FeatureFlagsError)
    assert installed_feature_flags() is None
    assert api.get_provider_metadata().name == "No-op Provider"  # the binding never started
