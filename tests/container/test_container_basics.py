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
"""Tests for DI container basic registration and resolution."""

import pytest

from pyfly.container import BeanCurrentlyInCreationError, Container, NoSuchBeanError, Scope


class Greeter:
    def greet(self) -> str:
        return "hello"


class UserService:
    def __init__(self, greeter: Greeter) -> None:
        self.greeter = greeter


# -- Helper classes for Optional injection tests (module-level for get_type_hints) --


class OptionalGreeterService:
    def __init__(self, greeter: Greeter | None = None) -> None:
        self.greeter = greeter


# -- Helper classes for list injection tests --


class Validator:
    pass


class EmailValidator(Validator):
    pass


class PhoneValidator(Validator):
    pass


class ValidationService:
    def __init__(self, validators: list[Validator]) -> None:
        self.validators = validators


# -- Helper classes for circular dependency tests --


class CircularB:
    def __init__(self, a: "CircularA") -> None:
        self.a = a


class CircularA:
    def __init__(self, b: CircularB) -> None:
        self.b = b


class ChainC:
    pass


class ChainB:
    def __init__(self, c: ChainC) -> None:
        self.c = c


class ChainA:
    def __init__(self, b: ChainB) -> None:
        self.b = b


class TestContainerBasics:
    def test_register_and_resolve(self):
        container = Container()
        container.register(Greeter)
        instance = container.resolve(Greeter)
        assert isinstance(instance, Greeter)
        assert instance.greet() == "hello"

    def test_resolve_with_dependency(self):
        container = Container()
        container.register(Greeter)
        container.register(UserService)
        service = container.resolve(UserService)
        assert isinstance(service, UserService)
        assert isinstance(service.greeter, Greeter)

    def test_singleton_scope_returns_same_instance(self):
        container = Container()
        container.register(Greeter, scope=Scope.SINGLETON)
        a = container.resolve(Greeter)
        b = container.resolve(Greeter)
        assert a is b

    def test_transient_scope_returns_new_instance(self):
        container = Container()
        container.register(Greeter, scope=Scope.TRANSIENT)
        a = container.resolve(Greeter)
        b = container.resolve(Greeter)
        assert a is not b

    def test_resolve_unregistered_raises(self):
        container = Container()
        with pytest.raises(NoSuchBeanError):
            container.resolve(Greeter)

    def test_bind_interface_to_implementation(self):
        class Cache:
            pass

        class RedisCache(Cache):
            pass

        container = Container()
        container.register(RedisCache)
        container.bind(Cache, RedisCache)
        instance = container.resolve(Cache)
        assert isinstance(instance, RedisCache)


class DefaultService:
    def __init__(self, name: str = "default") -> None:
        self.name = name


class TypeParamService:
    def __init__(self, model: type) -> None:
        self.model = model


class TestParameterDefaults:
    def test_respects_param_defaults(self):
        """Parameters with defaults are skipped when unresolvable."""
        container = Container()
        container.register(DefaultService)
        svc = container.resolve(DefaultService)
        assert svc.name == "default"

    def test_overrides_default_when_registered(self):
        """When the type IS registered, the resolved value overrides the default."""
        container = Container()
        container.register(Greeter)
        container.register(UserService)
        svc = container.resolve(UserService)
        assert isinstance(svc.greeter, Greeter)

    def test_type_param_raises(self):
        """type[T] parameters that can't be resolved raise NoSuchBeanError."""
        container = Container()
        container.register(TypeParamService)
        with pytest.raises(NoSuchBeanError):
            container.resolve(TypeParamService)


class PEP604OptionalService:
    """Uses PEP 604 union syntax (X | None) instead of typing.Optional."""

    def __init__(self, greeter: Greeter | None = None) -> None:
        self.greeter = greeter


class TestPEP604UnionType:
    def test_pep604_optional_resolves_to_none(self):
        """T | None (PEP 604) returns None when T is not registered."""
        container = Container()
        container.register(PEP604OptionalService)
        svc = container.resolve(PEP604OptionalService)
        assert svc.greeter is None

    def test_pep604_optional_resolves_to_instance(self):
        """T | None (PEP 604) returns the instance when T is registered."""
        container = Container()
        container.register(Greeter)
        container.register(PEP604OptionalService)
        svc = container.resolve(PEP604OptionalService)
        assert isinstance(svc.greeter, Greeter)


class TestOptionalInjection:
    def test_optional_resolves_to_none_when_missing(self):
        """Optional[T] returns None when T is not registered."""
        container = Container()
        container.register(OptionalGreeterService)
        svc = container.resolve(OptionalGreeterService)
        assert svc.greeter is None

    def test_optional_resolves_to_instance_when_registered(self):
        """Optional[T] returns the instance when T is registered."""
        container = Container()
        container.register(Greeter)
        container.register(OptionalGreeterService)
        svc = container.resolve(OptionalGreeterService)
        assert isinstance(svc.greeter, Greeter)
        assert svc.greeter.greet() == "hello"


class TestListInjection:
    def test_list_collects_all_implementations(self):
        """list[T] collects all implementations bound to T."""
        container = Container()
        container.register(EmailValidator)
        container.register(PhoneValidator)
        container.bind(Validator, EmailValidator)
        container.bind(Validator, PhoneValidator)
        container.register(ValidationService)
        svc = container.resolve(ValidationService)
        assert len(svc.validators) == 2
        types = {type(v) for v in svc.validators}
        assert types == {EmailValidator, PhoneValidator}

    def test_list_returns_empty_when_no_bindings(self):
        """list[T] returns an empty list when no implementations are bound."""
        container = Container()
        container.register(ValidationService)
        svc = container.resolve(ValidationService)
        assert svc.validators == []


class TestCircularDependencyDetection:
    def test_circular_dependency_raises(self):
        """Circular constructor dependencies raise BeanCurrentlyInCreationError."""
        container = Container()
        container.register(CircularA)
        container.register(CircularB)
        with pytest.raises(BeanCurrentlyInCreationError, match="Circular dependency"):
            container.resolve(CircularA)

    def test_non_circular_deep_chain_works(self):
        """A deep but non-circular chain resolves without error."""
        container = Container()
        container.register(ChainC)
        container.register(ChainB)
        container.register(ChainA)
        a = container.resolve(ChainA)
        assert isinstance(a.b.c, ChainC)
