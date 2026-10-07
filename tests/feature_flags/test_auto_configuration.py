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
from pyfly.container import component
from pyfly.container.bean import bean
from pyfly.container.container import Container
from pyfly.container.exceptions import BeanCreationException, NoSuchBeanError
from pyfly.container.stereotypes import configuration
from pyfly.context.application_context import ApplicationContext
from pyfly.context.events import app_event_listener
from pyfly.context.lifecycle import post_construct
from pyfly.core.config import Config
from pyfly.feature_flags.auto_configuration import FeatureFlagsAutoConfiguration
from pyfly.feature_flags.client import FeatureFlags, OpenFeatureBinding
from pyfly.feature_flags.context import EvaluationContextResolver, FeatureFlagsContextFilter
from pyfly.feature_flags.events import FeatureFlagEvaluated, FeatureFlagsChanged
from pyfly.feature_flags.hooks import EVALUATIONS_METRIC, ExposureEventHook, MetricsHook
from pyfly.feature_flags.provider import FireflyFlagProvider
from pyfly.feature_flags.registry import FEATURE_FLAGS_PHASE, FeatureFlagsError, FlagRegistry
from pyfly.feature_flags.slot import installed_feature_flags
from pyfly.kernel.lifecycle import lifecycle_phase
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


# -- the lifecycle: events reach the listeners, the application's beans see the flags ---------------------------


class FlagChanges:
    """Records every FeatureFlagsChanged its @app_event_listener receives."""

    def __init__(self) -> None:
        self.changes: list[FeatureFlagsChanged] = []

    @app_event_listener
    async def on_change(self, event: FeatureFlagsChanged) -> None:
        self.changes.append(event)


@component
class ScannedFlagChanges(FlagChanges):
    pass


class ProducedFlagChanges(FlagChanges):
    pass


@configuration
class ListenerConfiguration:
    @bean
    def produced_flag_changes(self) -> ProducedFlagChanges:
        return ProducedFlagChanges()


def _theme_file(theme: str) -> dict[str, Any]:
    """A flag file (plain flagd) whose ``theme`` flag answers *theme*."""
    return {"flags": {"theme": {"state": "ENABLED", "variants": {theme: theme}, "defaultVariant": theme}}}


@component
class ChangesTheFileBeforeTheContextIsRefreshed:
    """Between the lifecycle start (step 2e) and ContextRefreshedEvent (step 7): the flag file changes and the
    registry refreshes it, so a ``file`` change is published before any listener is wired."""

    def __init__(self, registry: FlagRegistry, config: Config) -> None:
        self._registry = registry
        self._path = Path(str(config.get("pyfly.feature-flags.sources.file.path")))

    @post_construct
    async def change_the_file(self) -> None:
        self._path.write_text(json.dumps(_theme_file("dark")), encoding="utf-8")
        assert await self._registry.refresh("file") == ["theme"]


async def test_listeners_receive_the_startup_change_once_and_before_any_file_change(tmp_path: Path) -> None:
    path = tmp_path / "flags.json"
    path.write_text(json.dumps(_theme_file("light")), encoding="utf-8")
    file = {"enabled": True, "path": str(path), "refresh-interval": "1h"}
    context = await _started(
        _config(flags={"a": True}, sources={"file": file}),
        ScannedFlagChanges,
        ListenerConfiguration,
        ChangesTheFileBeforeTheContextIsRefreshed,
    )
    expected = [FeatureFlagsChanged(("a", "theme"), "startup"), FeatureFlagsChanged(("theme",), "file")]
    assert context.get_bean(ScannedFlagChanges).changes == expected
    assert context.get_bean(ProducedFlagChanges).changes == expected  # a @bean product's listener is wired too
    path.write_text(json.dumps(_theme_file("blue")), encoding="utf-8")
    await context.get_bean(FlagRegistry).refresh("file")  # after the refresh: published at once
    assert context.get_bean(ScannedFlagChanges).changes[2:] == [FeatureFlagsChanged(("theme",), "file")]
    await context.stop()


class SeesTheFlags:
    """An application lifecycle bean that reads the flags (through the facade and the gating slot) when it starts and
    when it stops. It is created before the auto-configuration's beans, so only the phase orders it after them."""

    def __init__(self, container: Container) -> None:
        self._container = container
        self._facade: FeatureFlags | None = None
        self.seen: list[tuple[str, bool, bool]] = []

    async def start(self) -> None:
        self._facade = self._container.resolve(FeatureFlags)
        self._record("start")

    async def stop(self) -> None:
        self._record("stop")

    def _record(self, moment: str) -> None:
        assert self._facade is not None
        installed = installed_feature_flags()
        self.seen.append(
            (moment, self._facade.is_enabled("a"), installed is not None and installed.facade is self._facade)
        )


@configuration
class LifecycleConfiguration:
    @bean
    def sees_the_flags(self, container: Container) -> SeesTheFlags:
        return SeesTheFlags(container)


async def test_an_application_lifecycle_bean_sees_the_flags_when_it_starts_and_when_it_stops() -> None:
    context = await _started(_config(flags={"a": True}), LifecycleConfiguration)
    bean_instance = context.get_bean(SeesTheFlags)
    assert bean_instance.seen == [("start", True, True)]
    await context.stop()
    assert bean_instance.seen == [("start", True, True), ("stop", True, True)]


def test_the_registry_and_the_binding_share_the_feature_flags_phase() -> None:
    facade = FeatureFlags(OpenFeatureClient(domain="phase", version=None), EvaluationContextResolver())
    assert lifecycle_phase(OpenFeatureBinding(None, facade)) == FEATURE_FLAGS_PHASE
    assert lifecycle_phase(FlagRegistry([], FireflyFlagProvider())) == FEATURE_FLAGS_PHASE


async def test_the_context_stops_and_starts_again_with_the_feature_flags() -> None:
    context = await _started(_config(flags={"a": True}), ScannedFlagChanges)
    for _ in range(2):
        facade = context.get_bean(FeatureFlags)
        assert facade.is_enabled("a") is True
        assert api.get_provider_metadata().name == "firefly"
        installed = installed_feature_flags()
        assert installed is not None and installed.facade is facade
        assert context.get_bean(FlagRegistry).started
        assert context.get_bean(ScannedFlagChanges).changes == [FeatureFlagsChanged(("a",), "startup")]
        await context.stop()
        assert installed_feature_flags() is None
        assert api.get_provider_metadata().name == "No-op Provider"
        await context.start()
    await context.stop()


# -- an application's own OpenFeature client ---------------------------------------------------------------------


@configuration
class ApplicationClientConfiguration:
    @bean
    def payments_client(self) -> OpenFeatureClient:
        return OpenFeatureClient(domain="payments-app", version=None)


@component
class UsesTheOpenFeatureClient:
    def __init__(self, client: OpenFeatureClient) -> None:
        self.client = client


async def test_an_application_client_bean_backs_the_framework_client_bean_off() -> None:
    """The application's own ``OpenFeatureClient`` is the one bean of that type (no ambiguous dependency); the facade
    keeps the framework's client, with the Firefly hooks."""
    context = await _started(_config(flags={"a": True}), ApplicationClientConfiguration, UsesTheOpenFeatureClient)
    client = context.get_bean(OpenFeatureClient)
    assert client.domain == "payments-app"
    assert context.get_bean(UsesTheOpenFeatureClient).client is client
    facade = context.get_bean(FeatureFlags)
    assert facade.client is not client and facade.client.domain == "firefly"
    assert [type(hook) for hook in facade.client.hooks] == [MetricsHook]
    assert facade.is_enabled("a") is True
    await context.stop()


async def test_without_an_application_client_the_framework_client_is_the_bean() -> None:
    context = await _started(_config(flags={"a": True}))
    assert context.get_bean(OpenFeatureClient) is context.get_bean(FeatureFlags).client
    await context.stop()
