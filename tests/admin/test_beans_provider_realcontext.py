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
"""Robustness regression for BeansProvider against a REAL container.

The existing ``test_beans_provider_enhanced`` suite drives a *mocked* container
whose keys are always vanilla classes with ``__name__``/``__qualname__`` and whose
conditions are pre-sanitised. That blind spot let several admin-dashboard 500s ship
undetected. These tests register the awkward shapes a live container can actually
hold — union / ``TypeVar`` registration keys, generic-alias conditions, an
``on_class`` check that raises, and a class-level descriptor that raises — and assert
every admin endpoint returns a JSON-serialisable payload (i.e. no 500).
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

import pytest

from pyfly.admin.providers.beans_provider import BeansProvider
from pyfly.container.container import Container
from pyfly.container.types import Scope


class _ProviderCtx:
    def __init__(self, container: Container) -> None:
        self.container = container


def _provider(container: Container) -> BeansProvider:
    return BeansProvider(_ProviderCtx(container))  # type: ignore[arg-type]


class Alpha:
    pass


class Beta:
    pass


@pytest.mark.asyncio
async def test_union_registration_key_is_introspectable() -> None:
    c = Container()
    c.register(Alpha, scope=Scope.SINGLETON)
    c.register(Beta, scope=Scope.SINGLETON)
    c.register(Alpha | Beta, scope=Scope.SINGLETON)  # type: ignore[arg-type]
    provider = _provider(c)

    json.dumps(await provider.get_bean_graph())
    json.dumps(await provider.get_beans())


@pytest.mark.asyncio
async def test_typevar_registration_key_is_introspectable() -> None:
    t = TypeVar("T")
    c = Container()
    c.register(Alpha, scope=Scope.SINGLETON)
    c.register(t, scope=Scope.SINGLETON)  # type: ignore[arg-type]
    provider = _provider(c)

    json.dumps(await provider.get_bean_graph())
    json.dumps(await provider.get_beans())


@pytest.mark.asyncio
async def test_generic_alias_condition_is_json_serialisable() -> None:
    class Conditioned:
        pass

    # A @conditional_on_bean(list[str])-style condition stores a generic alias,
    # which is not JSON-serialisable unless coerced.
    Conditioned.__pyfly_conditions__ = [  # type: ignore[attr-defined]
        {"type": "on_bean", "bean_type": list[str]},
    ]
    c = Container()
    c.register(Conditioned, scope=Scope.SINGLETON)
    provider = _provider(c)

    detail = await provider.get_bean_detail("Conditioned")
    assert detail is not None
    json.dumps(detail)


@pytest.mark.asyncio
async def test_on_class_condition_check_that_raises_does_not_500() -> None:
    def _boom() -> bool:
        raise RuntimeError("condition check boom")

    class Flaky:
        pass

    Flaky.__pyfly_conditions__ = [  # type: ignore[attr-defined]
        {"type": "on_class", "module": "whatever", "check": _boom},
    ]
    c = Container()
    c.register(Flaky, scope=Scope.SINGLETON)
    provider = _provider(c)

    detail = await provider.get_bean_detail("Flaky")
    assert detail is not None
    json.dumps(detail)
    # The erroring check is reported as not-passed rather than crashing.
    assert detail["conditions"][0]["passed"] is False


@pytest.mark.asyncio
async def test_raising_descriptor_field_does_not_500() -> None:
    class RaisingDescriptor:
        def __get__(self, obj: Any, owner: Any = None) -> Any:
            raise ValueError("descriptor boom")

    class HasRaisingField:
        danger = RaisingDescriptor()

    c = Container()
    c.register(HasRaisingField, scope=Scope.SINGLETON)
    provider = _provider(c)

    detail = await provider.get_bean_detail("HasRaisingField")
    assert detail is not None
    json.dumps(detail)
