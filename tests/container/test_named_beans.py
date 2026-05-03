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
"""Tests for named beans, @primary resolution, and Qualifier injection."""

from typing import Annotated, Protocol, runtime_checkable

import pytest

from pyfly.container.bean import Qualifier, primary
from pyfly.container.container import Container
from pyfly.container.exceptions import NoSuchBeanError
from pyfly.container.stereotypes import component, service


@runtime_checkable
class Greeter(Protocol):
    def greet(self) -> str: ...


@component(name="english")
class EnglishGreeter:
    def greet(self) -> str:
        return "Hello"


@component(name="spanish")
@primary
class SpanishGreeter:
    def greet(self) -> str:
        return "Hola"


class TestNamedBeans:
    def test_register_and_resolve_by_name(self):
        c = Container()
        c.register(EnglishGreeter, name="english")
        result = c.resolve_by_name("english")
        assert result.greet() == "Hello"

    def test_resolve_by_name_not_found(self):
        c = Container()
        with pytest.raises(NoSuchBeanError, match="No bean named"):
            c.resolve_by_name("nonexistent")

    def test_resolve_all_of_type(self):
        c = Container()
        c.register(EnglishGreeter, name="english")
        c.register(SpanishGreeter, name="spanish")
        c.bind(Greeter, EnglishGreeter)
        c.bind(Greeter, SpanishGreeter)
        beans = c.resolve_all(Greeter)
        assert len(beans) >= 2


class TestPrimaryResolution:
    def test_primary_wins_when_multiple(self):
        c = Container()
        c.register(EnglishGreeter, name="english")
        c.register(SpanishGreeter, name="spanish")
        c.bind(Greeter, EnglishGreeter)
        c.bind(Greeter, SpanishGreeter)
        result = c.resolve(Greeter)
        assert result.greet() == "Hola"  # SpanishGreeter is @primary


class TestQualifierInjection:
    def test_qualifier_selects_named_bean(self):
        c = Container()
        c.register(EnglishGreeter, name="english")
        c.register(SpanishGreeter, name="spanish")
        c.bind(Greeter, EnglishGreeter)
        c.bind(Greeter, SpanishGreeter)

        @service
        class GreetService:
            def __init__(self, greeter: Annotated[Greeter, Qualifier("english")]):
                self.greeter = greeter

        c.register(GreetService)
        svc = c.resolve(GreetService)
        assert svc.greeter.greet() == "Hello"
