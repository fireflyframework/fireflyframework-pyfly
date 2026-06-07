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
"""Custom bean-scope SPI (v26.06.50): register_scope + ScopeHandler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pyfly.container.container import Container
from pyfly.container.types import ScopeHandler


class _DictScope:
    """A trivial ScopeHandler caching one instance per key."""

    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}

    def get(self, name: str, object_factory: Callable[[], Any]) -> Any:
        if name not in self.cache:
            self.cache[name] = object_factory()
        return self.cache[name]

    def remove(self, name: str) -> Any | None:
        return self.cache.pop(name, None)


class Widget:
    pass


def test_custom_scope_caches_via_handler() -> None:
    c = Container()
    c.register_scope("tenant", _DictScope())
    c.register(Widget, scope="tenant")
    first = c.resolve(Widget)
    assert isinstance(first, Widget)
    assert c.resolve(Widget) is first  # handler returns the cached instance


def test_unregistered_custom_scope_raises() -> None:
    c = Container()
    c.register(Widget, scope="ghost")
    with pytest.raises(RuntimeError, match="not registered"):
        c.resolve(Widget)


def test_register_scope_rejects_builtin_and_empty_names() -> None:
    c = Container()
    for reserved in ("singleton", "transient", "request", "session"):
        with pytest.raises(ValueError, match="built-in"):
            c.register_scope(reserved, _DictScope())
    with pytest.raises(ValueError, match="non-empty"):
        c.register_scope("", _DictScope())


def test_unregister_scope() -> None:
    c = Container()
    c.register_scope("tenant", _DictScope())
    c.register(Widget, scope="tenant")
    assert isinstance(c.resolve(Widget), Widget)
    c.unregister_scope("tenant")
    with pytest.raises(RuntimeError, match="not registered"):
        c.resolve(Widget)


def test_scope_handler_is_runtime_checkable() -> None:
    assert isinstance(_DictScope(), ScopeHandler)


def test_custom_scope_via_class_attribute() -> None:
    class Tenanted:
        __pyfly_scope__ = "tenant"

    c = Container()
    c.register_scope("tenant", _DictScope())
    c.register(Tenanted)
    assert isinstance(c.resolve(Tenanted), Tenanted)


def test_handler_remove_evicts() -> None:
    scope = _DictScope()
    c = Container()
    c.register_scope("tenant", scope)
    c.register(Widget, scope="tenant")
    first = c.resolve(Widget)
    key = next(iter(scope.cache))
    assert scope.remove(key) is first
    assert c.resolve(Widget) is not first  # rebuilt after eviction
