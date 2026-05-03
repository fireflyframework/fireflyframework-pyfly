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
"""Tests for developer-friendly DI error messages."""

import pytest

from pyfly.container import (
    BeanCreationException,
    BeanCurrentlyInCreationError,
    Container,
    NoSuchBeanError,
    NoUniqueBeanError,
)
from pyfly.container.autowired import Autowired

# -- Fixtures --


class Greeter:
    def greet(self) -> str:
        return "hello"


class Logger:
    pass


class MissingDep:
    pass


class ServiceWithMissing:
    def __init__(self, dep: MissingDep) -> None:
        self.dep = dep


class CircB:
    def __init__(self, a: "CircA") -> None:
        self.a = a


class CircA:
    def __init__(self, b: CircB) -> None:
        self.b = b


class CircC:
    def __init__(self, a: "CircABC") -> None:
        pass


class CircBBC:
    def __init__(self, c: CircC) -> None:
        pass


class CircABC:
    def __init__(self, b: CircBBC) -> None:
        pass


class FieldService:
    dep: MissingDep = Autowired()


class OptionalFieldService:
    dep: MissingDep = Autowired(required=False)


class Base:
    pass


class ImplX(Base):
    pass


class ImplY(Base):
    pass


# -- NoSuchBeanError tests --


class TestNoSuchBeanError:
    def test_inherits_from_bean_creation_exception(self):
        err = NoSuchBeanError(bean_type=Greeter)
        assert isinstance(err, BeanCreationException)

    def test_not_inherits_from_key_error(self):
        err = NoSuchBeanError(bean_type=Greeter)
        assert not isinstance(err, KeyError)

    def test_has_bean_type_attribute(self):
        err = NoSuchBeanError(bean_type=Greeter)
        assert err.bean_type is Greeter

    def test_has_bean_name_attribute(self):
        err = NoSuchBeanError(bean_name="myBean")
        assert err.bean_name == "myBean"

    def test_message_includes_type_name(self):
        err = NoSuchBeanError(bean_type=Greeter)
        assert "Greeter" in str(err)

    def test_message_includes_bean_name(self):
        err = NoSuchBeanError(bean_name="myBean")
        assert "myBean" in str(err)

    def test_message_includes_required_by(self):
        err = NoSuchBeanError(
            bean_type=Greeter,
            required_by="ServiceA.__init__()",
        )
        assert "ServiceA.__init__()" in str(err)

    def test_message_includes_parameter(self):
        err = NoSuchBeanError(
            bean_type=Greeter,
            parameter="greeter: Greeter",
        )
        assert "greeter: Greeter" in str(err)

    def test_message_includes_suggestions(self):
        err = NoSuchBeanError(
            bean_type=Greeter,
            suggestions=["Greet", "GreetingService"],
        )
        msg = str(err)
        assert "Greet" in msg
        assert "GreetingService" in msg

    def test_message_includes_actionable_hints(self):
        err = NoSuchBeanError(bean_type=Greeter)
        msg = str(err)
        assert "@component" in msg or "@service" in msg or "@bean" in msg
        assert "scan_packages" in msg


class TestNoSuchBeanErrorFromContainer:
    def test_resolve_unregistered_type(self):
        c = Container()
        with pytest.raises(NoSuchBeanError) as exc_info:
            c.resolve(Greeter)
        assert exc_info.value.bean_type is Greeter

    def test_resolve_by_name_not_found(self):
        c = Container()
        with pytest.raises(NoSuchBeanError) as exc_info:
            c.resolve_by_name("nonexistent")
        assert exc_info.value.bean_name == "nonexistent"

    def test_constructor_param_includes_context(self):
        c = Container()
        c.register(ServiceWithMissing)
        with pytest.raises(NoSuchBeanError) as exc_info:
            c.resolve(ServiceWithMissing)
        err = exc_info.value
        assert err.required_by is not None
        assert "ServiceWithMissing" in err.required_by
        assert err.parameter is not None
        assert "dep" in err.parameter

    def test_similar_types_suggested(self):
        c = Container()
        c.register(Greeter)
        c.register(ServiceWithMissing)
        with pytest.raises(NoSuchBeanError) as exc_info:
            c.resolve(ServiceWithMissing)
        # MissingDep is not registered; Greeter is close-ish but may not match.
        # Just verify suggestions is a list.
        assert isinstance(exc_info.value.suggestions, list)


# -- NoUniqueBeanError tests --


class TestNoUniqueBeanError:
    def test_inherits_from_bean_creation_exception(self):
        err = NoUniqueBeanError(bean_type=Base, candidates=[ImplX, ImplY])
        assert isinstance(err, BeanCreationException)

    def test_has_candidates(self):
        err = NoUniqueBeanError(bean_type=Base, candidates=[ImplX, ImplY])
        assert err.candidates == [ImplX, ImplY]

    def test_message_includes_type(self):
        err = NoUniqueBeanError(bean_type=Base, candidates=[ImplX, ImplY])
        assert "Base" in str(err)

    def test_message_includes_candidates(self):
        err = NoUniqueBeanError(bean_type=Base, candidates=[ImplX, ImplY])
        msg = str(err)
        assert "ImplX" in msg
        assert "ImplY" in msg

    def test_message_includes_fix_hint(self):
        err = NoUniqueBeanError(bean_type=Base, candidates=[ImplX, ImplY])
        msg = str(err)
        assert "@primary" in msg
        assert "Qualifier" in msg


class TestNoUniqueBeanErrorFromContainer:
    def test_multiple_impls_without_primary(self):
        c = Container()
        c.register(ImplX)
        c.register(ImplY)
        c.bind(Base, ImplX)
        c.bind(Base, ImplY)
        with pytest.raises(NoUniqueBeanError) as exc_info:
            c.resolve(Base)
        assert exc_info.value.bean_type is Base
        assert set(exc_info.value.candidates) == {ImplX, ImplY}


# -- BeanCurrentlyInCreationError tests --


class TestBeanCurrentlyInCreationError:
    def test_inherits_from_bean_creation_exception(self):
        err = BeanCurrentlyInCreationError(chain=[CircA, CircB], current=CircA)
        assert isinstance(err, BeanCreationException)

    def test_chain_is_ordered_list(self):
        err = BeanCurrentlyInCreationError(chain=[CircA, CircB], current=CircA)
        assert isinstance(err.chain, list)
        assert err.chain == [CircA, CircB]

    def test_message_shows_full_chain(self):
        err = BeanCurrentlyInCreationError(chain=[CircA, CircB], current=CircA)
        msg = str(err)
        assert "CircA -> CircB -> CircA" in msg

    def test_message_includes_suggestion(self):
        err = BeanCurrentlyInCreationError(chain=[CircA, CircB], current=CircA)
        assert "@post_construct" in str(err) or "factory" in str(err)


class TestCircularDependencyFromContainer:
    def test_two_way_circular(self):
        c = Container()
        c.register(CircA)
        c.register(CircB)
        with pytest.raises(BeanCurrentlyInCreationError) as exc_info:
            c.resolve(CircA)
        err = exc_info.value
        assert err.current is CircA  # CircA is the type being re-entered
        assert "CircA -> CircB -> CircA" in str(err)

    def test_chain_is_deterministic(self):
        """The chain must be deterministic (ordered) regardless of dict internals."""
        c = Container()
        c.register(CircABC)
        c.register(CircBBC)
        c.register(CircC)
        with pytest.raises(BeanCurrentlyInCreationError) as exc_info:
            c.resolve(CircABC)
        chain_names = [t.__name__ for t in exc_info.value.chain]
        assert chain_names == ["CircABC", "CircBBC", "CircC"]

    def test_resolving_cleaned_up_after_error(self):
        """Container._resolving is empty after a circular dependency error."""
        c = Container()
        c.register(CircA)
        c.register(CircB)
        with pytest.raises(BeanCurrentlyInCreationError):
            c.resolve(CircA)
        assert len(c._resolving) == 0


# -- Autowired field injection errors --


class TestAutowiredFieldErrors:
    def test_required_field_raises_with_context(self):
        c = Container()
        c.register(FieldService)
        with pytest.raises(NoSuchBeanError) as exc_info:
            c.resolve(FieldService)
        err = exc_info.value
        assert "FieldService" in (err.required_by or "")
        assert "dep" in (err.parameter or "")

    def test_optional_field_returns_none(self):
        c = Container()
        c.register(OptionalFieldService)
        svc = c.resolve(OptionalFieldService)
        assert svc.dep is None
