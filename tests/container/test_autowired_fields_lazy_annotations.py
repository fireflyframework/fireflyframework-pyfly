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
"""Field injection in a module WITHOUT ``from __future__ import annotations``.

On Python 3.14 (PEP 649/749) a class defined without the future import keeps no ``__annotations__``
in its ``__dict__``: the annotations are computed when they are first asked for. Field injection
read the class dictionaries directly, found no field at all, and left the ``Autowired`` sentinel on
every instance, silently. The fields are now found from the ``Autowired``/``Value`` objects the
class holds, and their annotations are read through the annotation API of the running Python.

This module must not import ``annotations`` from ``__future__``: the classes below are the ones a
3.14 user writes by default. The names only ``TYPE_CHECKING`` sees are quoted, because Python 3.12
and 3.13 evaluate the other annotations when the class is created.
"""

import logging
from typing import TYPE_CHECKING, Annotated

import pytest

from pyfly.container import Autowired, BeanCreationException, Container, Qualifier
from pyfly.core.config import Config
from pyfly.core.value import Value

if TYPE_CHECKING:
    from decimal import Decimal as _OnlyForTypeChecking


class Greeter:
    def greet(self) -> str:
        return "hello"


class _Service:
    greeter: Greeter = Autowired()
    named: Annotated[Greeter, Qualifier("main_greeter")] = Autowired()
    port: str = Value("${app.port:8080}")
    note: "_OnlyForTypeChecking | None" = None


class _Base:
    greeter: Greeter = Autowired()
    limit: "_OnlyForTypeChecking | None" = None


class _Child(_Base):
    port: str = Value("${app.port:8080}")


class _RequiredUnresolvable:
    amount: "_OnlyForTypeChecking" = Autowired()


class _OptionalUnresolvable:
    amount: "_OnlyForTypeChecking" = Autowired(required=False)


class _Unannotated:
    """Fields without an annotation: a qualifier or a ``Value`` needs none, a bare ``Autowired`` does."""

    named = Autowired(qualifier="main_greeter")
    port = Value("${app.port:8080}")


class _UnannotatedByType:
    greeter = Autowired()


def _container() -> Container:
    container = Container()
    container.register_instance(Config, Config({}))
    container.register(Greeter, name="main_greeter")
    return container


def test_every_field_of_a_class_with_lazy_annotations_is_injected() -> None:
    container = _container()
    container.register(_Service)

    service = container.resolve(_Service)

    assert isinstance(service.greeter, Greeter)
    assert isinstance(service.named, Greeter)
    assert service.port == "8080"
    assert service.note is None


def test_a_field_declared_on_a_base_class_is_injected() -> None:
    container = _container()
    container.register(_Child)

    child = container.resolve(_Child)

    assert isinstance(child.greeter, Greeter)
    assert child.port == "8080"


def test_a_required_field_whose_annotation_cannot_be_resolved_fails_the_creation() -> None:
    container = _container()
    container.register(_RequiredUnresolvable)

    with pytest.raises(BeanCreationException, match=r"_RequiredUnresolvable\.amount"):
        container.resolve(_RequiredUnresolvable)


def test_an_optional_field_whose_annotation_cannot_be_resolved_is_left_unset(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = _container()
    container.register(_OptionalUnresolvable)

    with caplog.at_level(logging.WARNING, logger="pyfly.container.container"):
        assert container.resolve(_OptionalUnresolvable).amount is None
    assert "_OptionalUnresolvable.amount" in caplog.text


def test_a_field_without_an_annotation_is_injected_when_it_needs_no_type() -> None:
    container = _container()
    container.register(_Unannotated)

    instance = container.resolve(_Unannotated)

    assert isinstance(instance.named, Greeter)
    assert instance.port == "8080"


def test_a_required_field_without_an_annotation_or_a_qualifier_fails_the_creation() -> None:
    """It used to keep the ``Autowired`` sentinel: the context booted and the first call failed."""
    container = _container()
    container.register(_UnannotatedByType)

    with pytest.raises(BeanCreationException, match=r"_UnannotatedByType\.greeter"):
        container.resolve(_UnannotatedByType)
