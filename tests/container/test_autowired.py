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
"""Tests for Autowired field injection."""

from typing import Annotated, Protocol, runtime_checkable

import pytest

from pyfly.container import Autowired, Container, NoSuchBeanError, Qualifier


class Greeter:
    def greet(self) -> str:
        return "hello"


class Logger:
    def log(self, msg: str) -> str:
        return msg


@runtime_checkable
class GreeterPort(Protocol):
    def greet(self) -> str: ...


# -- Module-level service classes for Autowired tests --
# (Must be at module level so get_type_hints can resolve annotations)


class BasicFieldService:
    greeter: Greeter = Autowired()


class QualifiedFieldService:
    greeter: Greeter = Autowired(qualifier="special_greeter")


class OptionalFieldService:
    greeter: Greeter = Autowired(required=False)


class RequiredFieldService:
    greeter: Greeter = Autowired()


class MixedInjectionService:
    logger: Logger = Autowired()

    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


class ProtocolFieldService:
    greeter: GreeterPort = Autowired()


class AnnotatedQualifierFieldService:
    greeter: Annotated[Greeter, Qualifier("named_greeter")] = Autowired()


class TestAutowiredBasic:
    def test_field_injection(self):
        """Autowired() resolves by type annotation."""
        container = Container()
        container.register(Greeter)
        container.register(BasicFieldService)
        svc = container.resolve(BasicFieldService)
        assert isinstance(svc.greeter, Greeter)
        assert svc.greeter.greet() == "hello"

    def test_field_injection_with_qualifier(self):
        """Autowired(qualifier=...) resolves by bean name."""
        container = Container()
        container.register(Greeter, name="special_greeter")
        container.register(QualifiedFieldService)
        svc = container.resolve(QualifiedFieldService)
        assert isinstance(svc.greeter, Greeter)

    def test_optional_field_not_registered(self):
        """Autowired(required=False) sets None when type is unresolvable."""
        container = Container()
        container.register(OptionalFieldService)
        svc = container.resolve(OptionalFieldService)
        assert svc.greeter is None

    def test_optional_field_when_registered(self):
        """Autowired(required=False) injects normally when type is available."""
        container = Container()
        container.register(Greeter)
        container.register(OptionalFieldService)
        svc = container.resolve(OptionalFieldService)
        assert isinstance(svc.greeter, Greeter)

    def test_required_field_raises_when_missing(self):
        """Autowired() (required=True) raises NoSuchBeanError when type is not registered."""
        container = Container()
        container.register(RequiredFieldService)
        with pytest.raises(NoSuchBeanError):
            container.resolve(RequiredFieldService)


class TestAutowiredMixed:
    def test_constructor_and_field_injection(self):
        """Constructor injection and field injection work on the same class."""
        container = Container()
        container.register(Greeter)
        container.register(Logger)
        container.register(MixedInjectionService)
        svc = container.resolve(MixedInjectionService)
        assert isinstance(svc.greeter, Greeter)
        assert isinstance(svc.logger, Logger)

    def test_autowired_with_protocol_type(self):
        """Autowired resolves Protocol-typed fields via bindings."""
        container = Container()
        container.register(Greeter)
        container.bind(GreeterPort, Greeter)
        container.register(ProtocolFieldService)
        svc = container.resolve(ProtocolFieldService)
        assert isinstance(svc.greeter, Greeter)
        assert svc.greeter.greet() == "hello"

    def test_autowired_with_annotated_qualifier(self):
        """Autowired on an Annotated[T, Qualifier] field uses the qualifier."""
        container = Container()
        container.register(Greeter, name="named_greeter")
        container.register(AnnotatedQualifierFieldService)
        svc = container.resolve(AnnotatedQualifierFieldService)
        assert isinstance(svc.greeter, Greeter)


class TestAutowiredRepr:
    def test_repr_default(self):
        assert repr(Autowired()) == "Autowired()"

    def test_repr_with_qualifier(self):
        assert repr(Autowired(qualifier="foo")) == "Autowired(qualifier='foo')"

    def test_repr_optional(self):
        assert repr(Autowired(required=False)) == "Autowired(required=False)"

    def test_repr_both(self):
        assert repr(Autowired(qualifier="x", required=False)) == "Autowired(qualifier='x', required=False)"
