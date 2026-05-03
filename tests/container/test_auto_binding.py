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
"""Tests for auto-binding interfaces during scanning."""

import abc
from typing import Protocol, runtime_checkable

import pytest

from pyfly.container import Container, NoUniqueBeanError, primary, service
from pyfly.container.scanner import _auto_bind_interfaces


@runtime_checkable
class GreeterPort(Protocol):
    def greet(self) -> str: ...


class AbstractRepo(abc.ABC):
    @abc.abstractmethod
    def find(self, id: int) -> dict: ...


class TestAutoBindProtocol:
    def test_auto_bind_to_protocol(self):
        """Scanning auto-binds a class that explicitly inherits a Protocol."""

        @service
        class GreeterImpl(GreeterPort):
            def greet(self) -> str:
                return "hello"

        container = Container()
        container.register(GreeterImpl)
        _auto_bind_interfaces(GreeterImpl, container)

        result = container.resolve(GreeterPort)
        assert isinstance(result, GreeterImpl)
        assert result.greet() == "hello"


class TestAutoBindABC:
    def test_auto_bind_to_abstract_base_class(self):
        """Scanning auto-binds a class to its abstract base class."""

        class RepoImpl(AbstractRepo):
            def find(self, id: int) -> dict:
                return {"id": id}

        container = Container()
        container.register(RepoImpl)
        _auto_bind_interfaces(RepoImpl, container)

        result = container.resolve(AbstractRepo)
        assert isinstance(result, RepoImpl)
        assert result.find(1) == {"id": 1}


class TestAutoBindMultipleImpls:
    def test_multiple_impls_with_primary(self):
        """When multiple impls are auto-bound, @primary disambiguates."""

        class ImplA(AbstractRepo):
            def find(self, id: int) -> dict:
                return {"source": "A"}

        @primary
        class ImplB(AbstractRepo):
            def find(self, id: int) -> dict:
                return {"source": "B"}

        container = Container()
        container.register(ImplA)
        _auto_bind_interfaces(ImplA, container)
        container.register(ImplB)
        _auto_bind_interfaces(ImplB, container)

        result = container.resolve(AbstractRepo)
        assert isinstance(result, ImplB)

    def test_resolve_all_collects_all_implementations(self):
        """resolve_all() returns all auto-bound implementations."""

        class ImplA(AbstractRepo):
            def find(self, id: int) -> dict:
                return {"source": "A"}

        class ImplB(AbstractRepo):
            def find(self, id: int) -> dict:
                return {"source": "B"}

        container = Container()
        container.register(ImplA)
        _auto_bind_interfaces(ImplA, container)
        container.register(ImplB)
        _auto_bind_interfaces(ImplB, container)

        results = container.resolve_all(AbstractRepo)
        assert len(results) == 2
        sources = {r.find(1)["source"] for r in results}
        assert sources == {"A", "B"}

    def test_multiple_impls_without_primary_raises(self):
        """Without @primary, resolving with multiple impls raises NoUniqueBeanError."""

        class ImplX(AbstractRepo):
            def find(self, id: int) -> dict:
                return {}

        class ImplY(AbstractRepo):
            def find(self, id: int) -> dict:
                return {}

        container = Container()
        container.register(ImplX)
        _auto_bind_interfaces(ImplX, container)
        container.register(ImplY)
        _auto_bind_interfaces(ImplY, container)

        with pytest.raises(NoUniqueBeanError, match="@primary"):
            container.resolve(AbstractRepo)
