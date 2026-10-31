# Copyright 2026 Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
"""Declared port aliases must preserve the candidate set and primary selection."""

from typing import Protocol, runtime_checkable

import pytest

from pyfly.container import Container, NoUniqueBeanError, bean, configuration, service
from pyfly.context import ApplicationContext
from pyfly.core import Config


@runtime_checkable
class DeliveryPort(Protocol):
    def label(self) -> str: ...


class FirstDelivery:
    def label(self) -> str:
        return "first"


class SecondDelivery:
    def label(self) -> str:
        return "second"


@service
class DeliveryConsumer:
    def __init__(self, delivery: DeliveryPort) -> None:
        self.delivery = delivery


@pytest.mark.parametrize("primary_flags", [(False, False), (True, False), (False, True), (True, True)])
async def test_declared_port_uses_all_candidates_and_exactly_one_primary(primary_flags):
    @configuration
    class Deliveries:
        @bean(primary=primary_flags[0])
        def first(self) -> DeliveryPort:
            return FirstDelivery()

        @bean(primary=primary_flags[1])
        def second(self) -> DeliveryPort:
            return SecondDelivery()

    context = ApplicationContext(Config({}), auto_configurations=[])
    context.register_bean(Deliveries)
    await context.start()
    try:
        assert len(context.container.resolve_all(DeliveryPort)) == 2
        if sum(primary_flags) == 1:
            selected = "first" if primary_flags[0] else "second"
            assert context.get_bean(DeliveryPort) is context.get_bean_by_name(selected)
        else:
            with pytest.raises(NoUniqueBeanError) as raised:
                context.get_bean(DeliveryPort)
            assert set(raised.value.candidate_names) == {"first", "second"}
            assert len(raised.value.primary_names) == sum(primary_flags)
    finally:
        await context.stop()


async def test_primary_other_implementation_wins_over_two_same_class_factories():
    @configuration
    class Deliveries:
        @bean
        def first(self) -> DeliveryPort:
            return FirstDelivery()

        @bean
        def another_first(self) -> DeliveryPort:
            return FirstDelivery()

        @bean(primary=True)
        def second(self) -> DeliveryPort:
            return SecondDelivery()

    context = ApplicationContext(Config({}), auto_configurations=[])
    context.register_bean(Deliveries)
    await context.start()
    try:
        assert len(context.container.resolve_all(DeliveryPort)) == 3
        assert context.get_bean(DeliveryPort) is context.get_bean_by_name("second")
    finally:
        await context.stop()


async def test_aliases_of_one_instance_are_one_candidate():
    shared = FirstDelivery()

    @configuration
    class Deliveries:
        @bean
        def first(self) -> DeliveryPort:
            return shared

        @bean
        def alias(self) -> DeliveryPort:
            return shared

    context = ApplicationContext(Config({}), auto_configurations=[])
    context.register_bean(Deliveries)
    await context.start()
    try:
        assert context.get_bean(DeliveryPort) is shared
        assert context.container.resolve_all(DeliveryPort) == [shared]
    finally:
        await context.stop()


@pytest.mark.parametrize("both_primary", [False, True])
async def test_ambiguous_port_factory_dependency_fails_startup(both_primary):
    @configuration
    class Deliveries:
        @bean(primary=both_primary)
        def first(self) -> DeliveryPort:
            return FirstDelivery()

        @bean(primary=both_primary)
        def second(self) -> DeliveryPort:
            return SecondDelivery()

        @bean
        def consumer(self, delivery: DeliveryPort) -> DeliveryConsumer:
            return DeliveryConsumer(delivery)

    context = ApplicationContext(Config({}), auto_configurations=[])
    context.register_bean(Deliveries)
    try:
        with pytest.raises(NoUniqueBeanError):
            await context.start()
    finally:
        await context.stop()


async def test_primary_alias_of_shared_instance_disambiguates_other_candidate():
    shared = FirstDelivery()

    @configuration
    class Deliveries:
        @bean
        def first(self) -> DeliveryPort:
            return shared

        @bean(primary=True)
        def preferred_alias(self) -> DeliveryPort:
            return shared

        @bean
        def second(self) -> DeliveryPort:
            return SecondDelivery()

    context = ApplicationContext(Config({}), auto_configurations=[])
    context.register_bean(Deliveries)
    await context.start()
    try:
        assert len(context.container.resolve_all(DeliveryPort)) == 2
        assert context.get_bean(DeliveryPort) is shared
    finally:
        await context.stop()


def test_explicit_port_registration_preserves_precedence_over_bindings():
    container = Container()
    explicit = FirstDelivery()
    container.register_instance(DeliveryPort, explicit)
    container.register(SecondDelivery)
    container.bind(DeliveryPort, SecondDelivery)
    assert container.resolve(DeliveryPort) is explicit


def test_two_primary_bound_implementations_are_ambiguous():
    container = Container()
    for implementation, name in ((FirstDelivery, "first"), (SecondDelivery, "second")):
        container.register(implementation, name=name)
        container.get_registration(implementation).primary = True
        container.bind(DeliveryPort, implementation)
    with pytest.raises(NoUniqueBeanError) as raised:
        container.resolve(DeliveryPort)
    assert set(raised.value.primary_names) == {"first", "second"}
