# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Configured policies wait for later auto-configurations instead of silently disappearing."""

from __future__ import annotations

import pytest

from pyfly.container import bean, configuration
from pyfly.container.exceptions import NoSuchBeanError, NoUniqueBeanError
from pyfly.context import ApplicationContext
from pyfly.context.conditions import auto_configuration
from pyfly.core.config import Config


class Provider:
    pass


class Policy:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider


class Consumer:
    def __init__(self, policy: Policy | None) -> None:
        self.policy = policy


consumer_calls: list[Policy | None] = []


@pytest.fixture(autouse=True)
def clear_calls():
    consumer_calls.clear()


@configuration
class UserPolicy:
    @bean
    def policy(self, provider: Provider) -> Policy:
        return Policy(provider)


@auto_configuration
class EarlyConsumer:
    @bean
    def consumer(self, policy: Policy | None = None) -> Consumer:
        consumer_calls.append(policy)
        return Consumer(policy)


@auto_configuration
class EarlyDefaultConsumer:
    @bean
    def consumer(self, policy: Policy = None) -> Consumer:
        consumer_calls.append(policy)
        return Consumer(policy)


@auto_configuration
class LateProvider:
    @bean
    def provider(self) -> Provider:
        return Provider()


@pytest.mark.parametrize("consumer_config", [EarlyConsumer, EarlyDefaultConsumer])
async def test_optional_configured_policy_waits_for_its_later_auto_configured_dependency(consumer_config):
    context = ApplicationContext(Config({}), auto_configurations=[consumer_config, LateProvider])
    context.register_bean(UserPolicy)
    try:
        await context.start()
        assert context.get_bean(Consumer).policy is context.get_bean(Policy)
        assert context.get_bean(Policy).provider is context.get_bean(Provider)
    finally:
        await context.stop()


@pytest.mark.parametrize("consumer_config", [EarlyConsumer, EarlyDefaultConsumer])
async def test_missing_configured_policy_dependency_fails_before_consumer_factory_runs(consumer_config):
    context = ApplicationContext(Config({}), auto_configurations=[consumer_config])
    context.register_bean(UserPolicy)
    with pytest.raises(NoSuchBeanError) as caught:
        await context.start()
    assert caught.value.bean_type is Provider
    assert caught.value.required_by == "UserPolicy.policy()"
    assert consumer_calls == []


async def test_absent_optional_policy_still_allows_the_consumer():
    context = ApplicationContext(Config({}), auto_configurations=[EarlyConsumer])
    try:
        await context.start()
        assert context.get_bean(Consumer).policy is None
    finally:
        await context.stop()


@pytest.mark.parametrize("consumer_config", [EarlyConsumer, EarlyDefaultConsumer])
async def test_ambiguous_transitive_factory_dependency_does_not_use_optional_default(consumer_config):
    @auto_configuration
    class AmbiguousProviders:
        @bean(name="one")
        def one(self) -> Provider:
            return Provider()

        @bean(name="two")
        def two(self) -> Provider:
            return Provider()

    context = ApplicationContext(Config({}), auto_configurations=[consumer_config, AmbiguousProviders])
    context.register_bean(UserPolicy)
    with pytest.raises(NoUniqueBeanError) as caught:
        await context.start()
    assert caught.value.bean_type is Provider
    assert consumer_calls == []


async def test_explicitly_declining_policy_still_allows_optional_consumer():
    @configuration
    class DecliningPolicy:
        @bean
        def policy(self, provider: Provider) -> Policy | None:
            return None

    context = ApplicationContext(Config({}), auto_configurations=[EarlyConsumer, LateProvider])
    context.register_bean(DecliningPolicy)
    try:
        await context.start()
        assert context.get_bean(Consumer).policy is None
        assert consumer_calls == [None]
    finally:
        await context.stop()
