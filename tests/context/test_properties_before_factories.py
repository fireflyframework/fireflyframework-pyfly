# Copyright 2026 Firefly Software Foundation & Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Configuration dependencies bind before eager user factories and public login hooks."""

from dataclasses import dataclass

from pyfly.container import Container, bean, configuration, service
from pyfly.context import ApplicationContext
from pyfly.context.conditions import auto_configuration, conditional_on_property
from pyfly.core import Config, config_properties
from pyfly.security.oauth2 import OAuth2AuthenticationSuccessHandler, OAuth2LoginHandler


@config_properties(prefix="early_properties")
@dataclass
class EarlyProperties:
    required: bool = False


@service
class EarlyDependency:
    def __init__(self, settings: EarlyProperties):
        self.required = settings.required


@dataclass
class EarlySnapshot:
    required: bool


@configuration
class EagerFactory:
    @bean
    def snapshot(self, dependency: EarlyDependency) -> EarlySnapshot:
        return EarlySnapshot(dependency.required)


class PropertiesSuccessHook(OAuth2AuthenticationSuccessHandler):
    def __init__(self, dependency: EarlyDependency):
        self.required = dependency.required

    async def on_success(self, request, registration, context) -> None:
        if not self.required:
            raise RuntimeError("Required configured policy was not bound")


@configuration
class HookFactory:
    @bean
    def success(self, dependency: EarlyDependency) -> OAuth2AuthenticationSuccessHandler:
        return PropertiesSuccessHook(dependency)


@config_properties(prefix="late_properties")
@dataclass
class LateProperties:
    value: str = "unbound"


class LateMarker:
    pass


@auto_configuration
class RegistersLateProperties:
    @bean
    def marker(self, container: Container) -> LateMarker:
        container.register(LateProperties)
        return LateMarker()


@conditional_on_property("excluded.enabled", having_value="true")
@config_properties(prefix="excluded")
@dataclass
class ExcludedProperties:
    value: str


def context(*classes, oauth=False):
    values = {
        "early_properties": {"required": True},
        "late_properties": {"value": "bound"},
        "pyfly": {"banner": {"mode": "off"}},
    }
    if oauth:
        values["pyfly"]["security"] = {
            "oauth2": {
                "login": {"enabled": True},
                "client": {
                    "enabled": True,
                    "registrations": {
                        "owned": {"client-id": "owned", "authorization-uri": "https://identity.test/auth"}
                    },
                },
            }
        }
    result = ApplicationContext(Config(values))
    for kind in classes:
        result.register_bean(kind)
    return result


async def test_eager_user_factory_receives_bound_dependent_properties_and_restart_rebinds(monkeypatch):
    ctx = context(EarlyProperties, EarlyDependency, EagerFactory)
    await ctx.start()
    try:
        first = ctx.get_bean(EarlyProperties)
        assert first.required and ctx.get_bean(EarlySnapshot).required
    finally:
        await ctx.stop()
    monkeypatch.setenv("PYFLY_EARLY_PROPERTIES_REQUIRED", "false")
    await ctx.start()
    try:
        assert ctx.get_bean(EarlyProperties) is not first
        assert not ctx.get_bean(EarlyProperties).required and not ctx.get_bean(EarlySnapshot).required
    finally:
        await ctx.stop()


async def test_native_optional_public_hook_graph_sees_bound_policy_before_login_factory():
    ctx = context(EarlyProperties, EarlyDependency, HookFactory, oauth=True)
    await ctx.start()
    try:
        hook = ctx.get_bean(OAuth2AuthenticationSuccessHandler)
        assert isinstance(hook, PropertiesSuccessHook) and hook.required
        assert ctx.get_bean(OAuth2LoginHandler)._authentication_success_handler is hook
    finally:
        await ctx.stop()


@configuration
class ExplicitPropertiesFactory:
    @bean
    def explicit_properties(self) -> EarlyProperties:
        return EarlyProperties(required=False)


async def test_early_and_late_binding_counts_accumulate_and_filtered_properties_stay_excluded():
    ctx = context(EarlyProperties, EarlyDependency, EagerFactory, RegistersLateProperties, ExcludedProperties)
    await ctx.start()
    try:
        assert ctx.get_bean(EarlySnapshot).required
        assert ctx.get_bean(LateProperties).value == "bound"
        assert ctx.wiring_counts["config_properties"] == 2
        assert not ctx.container.contains_type(ExcludedProperties)
    finally:
        await ctx.stop()


async def test_explicit_user_factory_and_prebuilt_instance_are_not_rebound():
    ctx = context(ExplicitPropertiesFactory, EarlyDependency, EagerFactory)
    await ctx.start()
    try:
        assert not ctx.get_bean(EarlyProperties).required and not ctx.get_bean(EarlySnapshot).required
    finally:
        await ctx.stop()
    ctx = context(EarlyDependency, EagerFactory)
    supplied = EarlyProperties(required=False)
    ctx.container.register_instance(EarlyProperties, supplied)
    await ctx.start()
    try:
        assert ctx.get_bean(EarlyProperties) is supplied
        assert not ctx.get_bean(EarlySnapshot).required
    finally:
        await ctx.stop()
