# Copyright 2026 Firefly Software Foundation. Licensed under the Apache License, Version 2.0.
"""Optional/default injection must not discard a configured bean's broken dependency graph."""

from __future__ import annotations

import pytest

from pyfly.container import Autowired
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError, NoUniqueBeanError


class Dependency:
    pass


class FirstDependency(Dependency):
    pass


class SecondDependency(Dependency):
    pass


class Policy:
    def __init__(self, dependency: Dependency) -> None:
        self.dependency = dependency


class FieldPolicy:
    dependency: Dependency = Autowired()


class OptionalFieldPolicyConsumer:
    def __init__(self, policy: FieldPolicy | None = None) -> None:
        self.policy = policy


class OptionalConsumer:
    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy


class DefaultConsumer:
    def __init__(self, policy: Policy = "absent") -> None:
        self.policy = policy


class FieldConsumer:
    policy: Policy = Autowired(required=False)


@pytest.mark.parametrize("consumer", [OptionalConsumer, DefaultConsumer, FieldConsumer])
def test_configured_policy_with_missing_transitive_dependency_never_becomes_optional(consumer):
    container = Container()
    container.register(Policy)
    container.register(consumer)
    with pytest.raises(NoSuchBeanError) as caught:
        container.resolve(consumer)
    assert caught.value.bean_type is Dependency
    assert caught.value.required_by == "Policy.__init__()"


@pytest.mark.parametrize("consumer", [OptionalConsumer, DefaultConsumer, FieldConsumer])
def test_configured_policy_with_ambiguous_transitive_dependency_never_becomes_optional(consumer):
    container = Container()
    container.register(Policy)
    container.register(FirstDependency)
    container.register(SecondDependency)
    container.bind(Dependency, FirstDependency)
    container.bind(Dependency, SecondDependency)
    container.register(consumer)
    with pytest.raises(NoUniqueBeanError) as caught:
        container.resolve(consumer)
    assert caught.value.bean_type is Dependency
    assert set(caught.value.candidates) == {FirstDependency, SecondDependency}


@pytest.mark.parametrize(
    "consumer,expected", [(OptionalConsumer, None), (DefaultConsumer, "absent"), (FieldConsumer, None)]
)
def test_genuinely_absent_policy_preserves_optional_or_default_behavior(consumer, expected):
    container = Container()
    container.register(consumer)
    assert container.resolve(consumer).policy == expected


@pytest.mark.parametrize("consumer", [OptionalConsumer, DefaultConsumer, FieldConsumer])
def test_available_configured_policy_is_injected(consumer):
    container = Container()
    container.register(Dependency)
    container.register(Policy)
    container.register(consumer)
    assert container.resolve(consumer).policy is container.resolve(Policy)


def test_missing_required_field_of_an_optional_configured_policy_is_not_swallowed():
    container = Container()
    container.register(FieldPolicy)
    container.register(OptionalFieldPolicyConsumer)
    with pytest.raises(NoSuchBeanError) as caught:
        container.resolve(OptionalFieldPolicyConsumer)
    assert caught.value.bean_type is Dependency
    assert caught.value.required_by == "FieldPolicy.dependency"


@pytest.mark.parametrize("consumer", [OptionalConsumer, DefaultConsumer, FieldConsumer])
def test_root_policy_ambiguity_preserves_existing_optional_default_behavior(consumer):
    container = Container()
    container.register_instance(Policy, Policy(Dependency()), name="one")
    container.register_instance(Policy, Policy(Dependency()), name="two")
    container.register(consumer)
    assert container.resolve(consumer).policy in (None, "absent")
